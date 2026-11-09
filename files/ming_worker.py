"""Child side of the shared-weights pool: receives CUDA IPC-mapped tensors from
tools/ming_weightd.py, binds them into the Ming model in place of per-process copies,
then boots ComfyUI inside this same process.

Registered as the package name `ming_worker` by main() so the node's hook
(import ming_worker) finds the very module object holding SHARED.
"""
import os
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:            # spawn re-imports this module at bootstrap on every OS:
    sys.path.insert(0, TOOLS)        # the shim must resolve without main() having ever run
import stack_paths as sp

ROOT = os.path.join(sp.COMFY_ROOT, "ComfyUI")
SHARED = None


# Quant metadata (weight_scale etc.) lives on the module as a plain attribute, outside
# _parameters/_buffers, so a state_dict walk misses it.
#
# Why the walk and not state_dict: ComfyUI's quant ops override state_dict to emit the
# CHECKPOINT representation (comfy/ops.py:1490-1492 _quantized_weight_state_dict, with
# _load_from_state_dict at 1489 rebuilding `weight` via _load_quantized_module). Aliasing
# those bridge tensors onto what the ops actually hold rebinds bytes that mean something
# else - the first attempt did exactly that and gave 4.1 s of audio instead of 42.1 s,
# deterministically, with no error raised. collect() and apply_shared() therefore walk the
# same containers on both sides and compare shape/stride/dtype/bytes before aliasing,
# raising on any mismatch instead of guessing.
EXTRA_ATTRS = ("weight_scale", "input_scale", "pre_quant_scale")


def _key(mod_name, attr):
    return (mod_name + "." + attr) if mod_name else attr


def collect(model):
    """Every tensor this model actually references, keyed by module path + attribute."""
    import torch
    out = {}
    for mod_name, mod in model.named_modules():
        for attr, val in list(getattr(mod, "_parameters", {}).items()):
            if isinstance(val, torch.Tensor):
                out[_key(mod_name, attr)] = val
        for attr, val in list(getattr(mod, "_buffers", {}).items()):
            if isinstance(val, torch.Tensor):
                out[_key(mod_name, attr)] = val
        for attr in EXTRA_ATTRS:
            val = getattr(mod, attr, None)
            if isinstance(val, torch.Tensor):
                out.setdefault(_key(mod_name, attr), val)
    return out


def _inner_fields(t):
    """(inner_names, ctx, {name: tensor}) for a tensor subclass, else (None, None, None).

    comfy_kitchen's QuantizedTensor (168 of this model's tensors, 0.72 GB) pickles its
    storages directly, which is what raised reduce_storage / "Cannot pickle CUDA storage".
    __tensor_flatten__ returns (inner attribute names, context dict) - and the context
    dict, NOT the names, is what __tensor_unflatten__ wants first: passing the wrong half
    fails with KeyError: 'non_tensor_fields'."""
    flat = getattr(t, "__tensor_flatten__", None)
    if flat is None:
        return None, None, None
    names, ctx = flat()
    try:
        return list(names), ctx, {n: getattr(t, n) for n in names}
    except AttributeError:
        return None, None, None


def _to_dev(t, dev):
    if t.requires_grad:
        t = t.detach()                       # non-leaf tensors cannot cross processes
    return t if t.is_cuda else t.to(dev)


def pack(model, dev, stream=None):
    """Everything this model holds, flattened for transport: plain tensors as-is,
    quantized subclasses as their inner tensors plus the ctx needed to rebuild them.

    stream=True (opt-in; MING_PACK_STREAM=1) also rebinds each tensor into the model the
    instant it is published, so this process's HOST copy is released immediately instead of
    sitting next to the GPU copy until the whole model has been published. MEASURED NEUTRAL,
    so it is off by default: holder commit reads 5.9 after load, 8.6 after pack, 6.0 after
    bind - identical with the flag on and off. Releasing the host tensors early does not
    lower commit because the Windows heap keeps freed arenas charged to the process; the
    bytes become reusable but the reservation stays. Cutting that 2.6 GB transient for real
    would mean never materialising the host copy at all - loading straight onto the device
    inside the node's loader - not freeing it sooner. Kept because it is harmless and makes
    that experiment cheap to run."""
    if stream is None:
        stream = os.environ.get("MING_PACK_STREAM", "0") != "0"
    tensors, meta, cls = {}, {}, {}
    for name, t in list(collect(model).items()):      # snapshot: streaming mutates as we go
        fields_names, ctx, fields = _inner_fields(t)
        if ctx is None:
            v = _to_dev(t, dev)
            tensors[name] = v
            if stream:
                _install(model, name, v, False)
            continue
        meta[name] = (fields_names, ctx)
        # The class has to travel by name: a worker that skips load_weights has no local
        # instance of this subclass to take type(t) from - its skeleton is still on the meta
        # device, where the weight is a plain placeholder, not a QuantizedTensor.
        cls[name] = "%s:%s" % (type(t).__module__, type(t).__qualname__)
        inner = {}
        for n, v in fields.items():
            g = _to_dev(v, dev)
            tensors["%s::%s" % (name, n)] = g
            inner[n] = g
        if stream:
            # Rebuild the subclass here from the already-moved inner storages; the old host
            # object and its storages lose their last reference and go. If this fails, the
            # unpack() pass that follows does the identical job and reports the problems,
            # so streaming can never silently produce a worse result than not streaming.
            try:
                _install(model, name, type(t).__tensor_unflatten__(inner, ctx, None, None), True)
            except Exception:
                pass
    return {"t": tensors, "m": meta, "c": cls}


def _resolve_cls(ref):
    """Import the 'module:Qualname' reference pack() recorded for each quant subclass.
    Needed because a worker that skips loading has no local instance to take type(t) from."""
    if not ref:
        return None
    try:
        modname, _, qual = ref.partition(":")
        obj = __import__(modname, fromlist=["_"])
        for part in qual.split("."):
            obj = getattr(obj, part)
        return obj
    except Exception:
        return None


def _install(model, name, src, quant):
    """Put a shared tensor into whatever container this model uses for that name.
    Returns False when the module path itself does not exist here.

    The 'slot does not exist yet' case is not hypothetical: comfy's quantized ops create
    their `weight` inside _load_from_state_dict (_load_quantized_module), so a model that
    was only built on the meta device has Linear modules with no weight parameter at all.
    Upstream wraps the quant tensor in a Parameter there, so a fresh slot is wrapped the
    same way; an existing slot keeps the container it already had."""
    parent, _, attr = name.rpartition(".")
    import torch                                  # this module has no module-level torch:
    try:                                          # collect()/unpack() import it per function,
                                                  # and a helper that forgets to dies with
                                                  # NameError only on the Parameter paths
        mod = model.get_submodule(parent) if parent else model
    except Exception:
        return False
    params = getattr(mod, "_parameters", {})
    if attr in params:
        if isinstance(params[attr], torch.nn.Parameter) and not quant:
            params[attr] = torch.nn.Parameter(src, requires_grad=False)
        else:
            params[attr] = src
    elif attr in getattr(mod, "_buffers", {}):
        mod._buffers[attr] = src
    elif attr not in params and quant:
        try:
            mod._parameters[attr] = torch.nn.Parameter(src, requires_grad=False)
        except Exception:
            setattr(mod, attr, src)
    else:
        setattr(mod, attr, src)
    return True


def unpack(payload, model, strict=True):
    """Rebuild/alias payload tensors into this model's containers. A plain tensor is
    aliased to the holder's storage (no copy); a subclass is rebuilt from shared inner
    tensors, which still reference that same storage."""
    import torch
    tensors, meta = payload["t"], payload["m"]
    bad = []
    bound = 0
    for name, t in collect(model).items():
        parent, _, attr = name.rpartition(".")
        mod = model.get_submodule(parent) if parent else model
        ctx = meta.get(name)
        if ctx is not None:
            names, ctx = ctx
            klass = _resolve_cls(payload.get("c", {}).get(name))
            if klass is None and not t.is_meta:
                klass = type(t)             # the loaded-weight path still works unchanged
            if klass is None:
                bad.append("%s: no class reference and a meta placeholder to take it from" % name)
                continue
            try:
                # base.py:346-360 creates the _param_* attributes lazily, inside
                # __tensor_flatten__ itself, so a freshly loaded instance in this process
                # does not expose them until flatten has been called once on it.
                t.__tensor_flatten__()
            except Exception:
                pass
            inner = {}
            for n in names:
                s = tensors.get("%s::%s" % (name, n))
                v = getattr(t, n, None)
                if s is None:
                    bad.append("%s::%s missing from holder" % (name, n))
                    break
                # Against a loaded worker we can compare the holder's inner tensor with
                # this process's own; against a meta skeleton there is nothing local to
                # compare, so the rebuilt object's logical shape is checked below instead.
                if v is not None and (tuple(s.shape) != tuple(v.shape) or s.dtype != v.dtype):
                    bad.append("%s::%s %s/%s vs holder %s/%s"
                               % (name, n, tuple(v.shape), v.dtype, tuple(s.shape), s.dtype))
                    break
                inner[n] = s
            else:
                try:
                    # comfy_kitchen/tensor/base.py:369 - signature is
                    # __tensor_unflatten__(inner_tensors, ctx, outer_size, outer_stride):
                    # inner tensors FIRST, context second. Swapping them raises
                    # KeyError: 'non_tensor_fields' on the names list.
                    src = klass.__tensor_unflatten__(inner, ctx, None, None)
                except Exception as exc:
                    bad.append("unflatten %s: %s: %s" % (name, type(exc).__name__, str(exc)[:120]))
                    continue
                if tuple(src.shape) != tuple(t.shape) or (not t.is_meta and src.dtype != t.dtype):
                    bad.append("%s rebuilt as %s/%s but the skeleton wants %s/%s"
                               % (name, tuple(src.shape), src.dtype, tuple(t.shape), t.dtype))
                    continue
        else:
            src = tensors.get(name)
            if src is None:
                bad.append("%s absent from holder" % name)
                continue
            # A meta skeleton reports the op's master-weight dtype, not the dtype the loaded
            # model ends up with: MixedPrecision ops are built with float32 placeholders and
            # load bf16 values, so comparing dtype here would reject 198 perfectly good
            # tensors. Shape and stride come from the config, so those stay checked either
            # way; dtype/byte-size is checked only against a tensor this process actually
            # materialised.
            if tuple(src.shape) != tuple(t.shape) or src.stride() != t.stride():
                bad.append("%s mismatch this=%s/%s holder=%s/%s"
                           % (name, tuple(t.shape), t.stride(), tuple(src.shape), src.stride()))
                continue
            if not t.is_meta and (src.dtype != t.dtype
                                  or src.numel() * src.element_size() != t.numel() * t.element_size()):
                bad.append("%s dtype mismatch this=%s holder=%s" % (name, t.dtype, src.dtype))
                continue
        if attr in getattr(mod, "_parameters", {}):
            cur = mod._parameters[attr]
            if isinstance(cur, torch.nn.Parameter) and ctx is None:
                mod._parameters[attr] = torch.nn.Parameter(src, requires_grad=False)
            else:
                mod._parameters[attr] = src
        elif attr in getattr(mod, "_buffers", {}):
            mod._buffers[attr] = src
        else:
            setattr(mod, attr, src)
        bound += 1
    # Second pass: the payload, not this model, is the source of truth. comfy's quantized op
    # modules do not own a `weight` until a checkpoint load creates one, so a meta-built
    # skeleton has modules with no slot for those tensors at all, and a walk of this model
    # alone silently leaves them empty - which surfaces much later, in ComfyUI's
    # ModelPatcher.get_key_weight, as "'Linear' object has no attribute 'weight''. Anything
    # the holder carries and this model lacks is installed the way upstream's loader would
    # have created it. Validation is necessarily weaker here - there is nothing local to
    # compare - so the ctx that comfy_kitchen recorded at quantize time (orig_shape) is the
    # check that remains, on top of the md5 regression test that catches a wrong
    # representation, which is how the state_dict-bridge bug was found in the first place.
    done = set(collect(model))
    for name, conf in meta.items():
        if name in done:
            continue
        names, ctx = conf
        klass = _resolve_cls(payload.get("c", {}).get(name))
        if klass is None:
            bad.append("%s: no class reference for a slot this model never created" % name)
            continue
        inner = {}
        for n in names:
            s = tensors.get("%s::%s" % (name, n))
            if s is None:
                bad.append("%s::%s missing from holder" % (name, n))
                break
            inner[n] = s
        else:
            try:
                src = klass.__tensor_unflatten__(inner, ctx, None, None)
            except Exception as exc:
                bad.append("unflatten %s: %s: %s" % (name, type(exc).__name__, str(exc)[:120]))
                continue
            want = ctx.get("orig_shape") if isinstance(ctx, dict) else None
            if want and tuple(src.shape) != tuple(want):
                bad.append("%s rebuilt %s but its quantize ctx says %s"
                           % (name, tuple(src.shape), tuple(want)))
                continue
            if _install(model, name, src, True):
                bound += 1
            else:
                bad.append("%s: module path absent from this model" % name)
    for name, src in tensors.items():
        if "::" in name or name in done:
            continue
        if _install(model, name, src, False):
            bound += 1
        else:
            bad.append("%s: module path absent from this model" % name)
    if bad and strict:
        raise RuntimeError("shared weights incompatible with this model (%d problem(s)): %s"
                           % (len(bad), bad[:8]))
    return bound, len(bad)


def main(port, q):
    global SHARED
    # 0xC0000409 is raised as STATUS_STACK_BUFFER_OVERRUN, and faulthandler's Windows
    # vectored-exception handler catches it: this is the only way the fp32 shared-pool crash
    # gets a Python frame instead of silence (no ComfyUI traceback ever reached the logs).
    import faulthandler
    faulthandler.enable(all_threads=True)
    import torch.multiprocessing           # registers the CUDA tensor reducers for q.get()
    if os.environ.get("MING_SHARE_DISABLE") == "1":
        SHARED = None                      # control run: boot inside the holder but load our
    else:                                  # own weights, to prove the delta is IPC mapping
        SHARED = q.get()                   # maps the holder's memory, does not copy it
    os.environ["MING_SHARE"] = "1"
    sys.modules.setdefault("ming_worker", sys.modules[__name__])
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    print("worker %d mapped %s"
          % (port, "nothing (MING_SHARE_DISABLE=1, loading privately)" if SHARED is None
             else "%d tensors + %d quant subclass(es)" % (len(SHARED["t"]), len(SHARED["m"]))),
          flush=True)
    import runpy
    os.chdir(ROOT)
    # MING_WORKER_FLAGS: space-separated ComfyUI flags; "none" means boot with none at all
    # (drops --highvram, i.e. lets DynamicVRAM stream the shared tensors) for diagnosis.
    _f = os.environ.get("MING_WORKER_FLAGS")
    flags = [] if _f == "none" else (_f.split() if _f else ["--highvram"])
    sys.argv = ["main.py", "--port", str(port)] + flags
    runpy.run_path(os.path.join(ROOT, "main.py"), run_name="__main__")


if __name__ == "__main__":
    pass                                     # spawn re-imports this module: no side effects

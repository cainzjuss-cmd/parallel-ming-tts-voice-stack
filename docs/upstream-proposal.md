# Upstream proposal: a shared-weights hook in `ComfyUI-Ming-3-tts/loader.py`

Target: https://github.com/Saganaki22/ComfyUI-Ming-3-tts (MIT, port of
[inclusionAI/Ming-3.0-tts](https://github.com/inclusionAI/Ming-3.0-tts)).
Local node at the time of writing: v1.0.0 equivalent, no git metadata (installed as an archive),
running on ComfyUI 0.44.0, Windows, RTX 4090.

## Why

A voice-reply agent needs N ComfyUI instances so that the sentences of one answer synthesize in
parallel. On this card each instance pays its own copy of the model: 2.6 GB (0.5B INT8 ConvRot)
or 4.8 GB (fp32) per process, plus - the part that actually kills the machine - **several GB of
private host commit per process**, because each worker dequantizes its own copy of the checkpoint
and keeps the host side alive. Three fp32 workers reached the machine's commit limit and died
with a native `0xC0000409` fastfail, no Python exception, nothing in the log.

One process loads the model once; the others map the *same* GPU tensors through CUDA IPC. That
turned three fp32 workers from a crash into `19.4 GB peak, 0 deaths`, and made `4, 5, 6` workers
render correctly as well (`+435 / +480 / +444 MiB` whole-batch card delta). Measured marginal
cost per extra worker: **0.07-0.11 GB of VRAM** (activations only) instead of a whole weight set.

The change needed in this node is about ten lines, because everything else can live in a separate
module.

## Proposed patch (shape, not a literal diff to apply blind)

In `loader.py`, where the single line `load_weights(model, path, dtype)` binds the checkpoint into
the meta-built model, allow an externally supplied payload to bind it instead:

```python
    # Optional: bind tensors owned elsewhere (e.g. a CUDA-IPC weight holder) instead of
    # reading the checkpoint. `ming_worker.SHARED` is populated in each worker process by
    # the holder before ComfyUI boots; when absent, behaviour is completely unchanged.
    shared = None
    if os.environ.get("MING_SHARE"):
        try:
            import ming_worker                      # lives on sys.path, shipped alongside
            shared = getattr(ming_worker, "SHARED", None)
        except Exception as exc:
            raise RuntimeError("MING_SHARE is set but ming_worker did not import: %s" % exc)

    if shared is not None:
        bound, bad = ming_worker.unpack(shared, model)   # creates the quant op slots the meta
    else:                                                # skeleton is missing, then binds
        load_weights(model, path, dtype)
```

Notes for review:

- The model is built on `torch.device("meta")`, so `unpack` must **create** the missing
  `_parameters` entries (wrapping quantized tensors in `Parameter`, exactly as
  `_load_quantized_module` does) before binding. Doing only a name-by-name rebind silently
  leaves ops unbuilt.
- Skipping `load_weights` is safe with respect to `comfy_quant`: that metadata is consulted only
  inside `_load_from_state_dict` (`comfy/ops.py` ~1285/1660/1772), never at runtime. Its effect is
  carried in the `QuantizedTensor` objects themselves, which the payload transports. A
  `MING_KEEP_LOAD=1` escape hatch that does load-then-swap is worth keeping for A/B and for
  recovery if a quant library changes underneath.
- A log line confirming which path a boot took is worth having, but it should be opt-in or routed
  through a config value: writes from inside a node are swallowed by ComfyUI's stdout tee, so
  `print` is not sufficient evidence and a hardcoded path is not acceptable upstream.

## The traps this cost (the useful part, if anyone else attempts it)

1. **Sharing the checkpoint representation gives wrong audio with no error.** Rebinding through
   `state_dict()`/`load_state_dict(assign=True)` produced deterministic, plausible output of the
   wrong length (4.1 s where 42.1 s was expected) because the quant ops' state-dict bridge emits a
   different tensor form than the one the model runs on. Share the **runtime** tensors
   (`_parameters` / `_buffers` / the quant op's own attributes: `weight_scale`, `input_scale`,
   `pre_quant_scale`).
2. **`comfy_kitchen` `QuantizedTensor` will not pickle through IPC** (`Cannot pickle CUDA storage`
   in `reduce_storage`). It has to be flattened: `__tensor_flatten__()` returns inner names
   (`_qdata`, `_param_scale`) plus context, and `__tensor_unflatten__(inner, ctx, outer_size,
   outer_stride)` rebuilds it - inner tensors first, and the inner attributes exist only *after*
   `__tensor_flatten__()` has run on that instance.
3. **Plain `multiprocessing` cannot send CUDA tensors**; use `torch.multiprocessing`. Also
   `detach()` before transport, and remember a meta placeholder reports the *op's master dtype*
   (fp32 for a MixedPrecision op), so dtype validation must apply only to materialised tensors.
4. **`multiprocessing.spawn` re-imports the launcher module on Windows**, so any module-level
   import that pulls in `comfy.cli_args` parses the *child's* argv - every worker booted on the
   same port until `sys.argv = [sys.argv[0]]` was set and the imports were deferred.
5. The driver emits a real warning once a consumer maps over 1000 producer allocations
   ("Producer process tried to deallocate over 1000 memory blocks referred by consumer
   processes"). Harmless here, but worth knowing it is expected, not a sign of corruption.

## What I would offer with it

`ming_worker.py` (pack/unpack + the IPC transport rules above), `ming_weightd.py` (the holder
that loads once and spawns children), and `cap_probe.py` (per-worker VRAM/commit measurement, so
a multi-worker pool can be sized from numbers instead of trial and error). If a hook in the node
is not wanted, the same three files work as long as the node can be made to skip its own weight
load by any other means.

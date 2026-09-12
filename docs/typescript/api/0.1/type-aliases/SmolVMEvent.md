[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SmolVMEvent

# Type Alias: SmolVMEvent

> **SmolVMEvent** = \{ `type`: `"runtime.starting"`; \} \| \{ `protocolVersion`: `number`; `type`: `"runtime.ready"`; \} \| \{ `error`: [`SmolVMError`](../classes/SmolVMError.md); `type`: `"runtime.error"`; \} \| \{ `image`: `string`; `receivedBytes`: `number`; `totalBytes?`: `number`; `type`: `"image.download"`; \} \| \{ `type`: `"sandbox.starting"`; \} \| \{ `sandboxId`: `string`; `type`: `"sandbox.ready"`; \} \| \{ `sandboxId`: `string`; `type`: `"sandbox.deleted"`; \} \| \{ `sandboxId`: `string`; `type`: `"command.started"`; \} \| \{ `result`: [`ExecResult`](../interfaces/ExecResult.md); `sandboxId`: `string`; `type`: `"command.completed"`; \}

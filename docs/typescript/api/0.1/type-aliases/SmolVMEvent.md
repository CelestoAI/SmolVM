[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SmolVMEvent

# Type Alias: SmolVMEvent

> **SmolVMEvent** = \{ `type`: `"runtime.starting"`; \} \| \{ `protocolVersion`: `number`; `type`: `"runtime.ready"`; \} \| \{ `error`: [`SmolVMError`](../classes/SmolVMError.md); `type`: `"runtime.error"`; \} \| \{ `image`: `string`; `receivedBytes`: `number`; `totalBytes?`: `number`; `type`: `"image.download"`; \} \| \{ `type`: `"sandbox.starting"`; \} \| \{ `sandboxId`: `string`; `type`: `"sandbox.ready"`; \} \| \{ `sandboxId`: `string`; `type`: `"sandbox.deleted"`; \} \| \{ `sessionId`: `string`; `type`: `"browser.starting"`; \} \| \{ `sandboxId`: `string`; `sessionId`: `string`; `type`: `"browser.ready"`; \} \| \{ `sandboxId`: `string`; `sessionId`: `string`; `type`: `"browser.stopping"`; \} \| \{ `sandboxId`: `string`; `sessionId`: `string`; `type`: `"browser.deleted"`; \} \| \{ `computerId`: `string`; `type`: `"computer.starting"`; \} \| \{ `computerId`: `string`; `sandboxId`: `string`; `type`: `"computer.ready"`; \} \| \{ `computerId`: `string`; `message`: `string`; `process`: `string`; `sandboxId`: `string`; `type`: `"computer.error"`; \} \| \{ `computerId`: `string`; `sandboxId`: `string`; `type`: `"computer.stopping"`; \} \| \{ `computerId`: `string`; `sandboxId`: `string`; `type`: `"computer.deleted"`; \} \| \{ `sandboxId`: `string`; `type`: `"command.started"`; \} \| \{ `result`: [`ExecResult`](../interfaces/ExecResult.md); `sandboxId`: `string`; `type`: `"command.completed"`; \}

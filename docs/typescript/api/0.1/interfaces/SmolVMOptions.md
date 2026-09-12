[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SmolVMOptions

# Interface: SmolVMOptions

## Properties

### debug?

> `optional` **debug?**: `boolean`

Retain non-enumerable causes on SmolVMError instances.

***

### onEvent?

> `optional` **onEvent?**: (`event`) => `void`

Observe typed lifecycle events.

#### Parameters

##### event

[`SmolVMEvent`](../type-aliases/SmolVMEvent.md)

#### Returns

`void`

***

### runtimePath?

> `optional` **runtimePath?**: `string`

Runtime executable path. Defaults to `smolvm` on PATH.

***

### startupTimeoutMs?

> `optional` **startupTimeoutMs?**: `number`

Time allowed for the local bridge to start.

***

### transport?

> `optional` **transport?**: [`SmolVMTransport`](SmolVMTransport.md)

Supply a structural transport in tests; normal applications should omit this.

[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SmolVMClient

# Interface: SmolVMClient

The mockable client contract for creating sandboxes, diagnosing setup, and cleaning up.

## Properties

### sandboxes

> `readonly` **sandboxes**: [`SandboxCollection`](SandboxCollection.md)

## Methods

### close()

> **close**(): `Promise`\<`void`\>

#### Returns

`Promise`\<`void`\>

***

### diagnose()

> **diagnose**(): `Promise`\<[`DiagnoseResult`](DiagnoseResult.md)\>

#### Returns

`Promise`\<[`DiagnoseResult`](DiagnoseResult.md)\>

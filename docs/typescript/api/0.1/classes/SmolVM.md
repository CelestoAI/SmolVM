[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SmolVM

# Class: SmolVM

Entry point for creating disposable local sandboxes.

## Implements

- [`SmolVMClient`](../interfaces/SmolVMClient.md)

## Constructors

### Constructor

> **new SmolVM**(`options?`): `SmolVM`

#### Parameters

##### options?

[`SmolVMOptions`](../interfaces/SmolVMOptions.md) = `{}`

#### Returns

`SmolVM`

## Properties

### sandboxes

> `readonly` **sandboxes**: [`SandboxCollection`](../interfaces/SandboxCollection.md)

#### Implementation of

[`SmolVMClient`](../interfaces/SmolVMClient.md).[`sandboxes`](../interfaces/SmolVMClient.md#sandboxes)

## Methods

### close()

> **close**(): `Promise`\<`void`\>

#### Returns

`Promise`\<`void`\>

#### Implementation of

[`SmolVMClient`](../interfaces/SmolVMClient.md).[`close`](../interfaces/SmolVMClient.md#close)

***

### diagnose()

> **diagnose**(): `Promise`\<[`DiagnoseResult`](../interfaces/DiagnoseResult.md)\>

#### Returns

`Promise`\<[`DiagnoseResult`](../interfaces/DiagnoseResult.md)\>

#### Implementation of

[`SmolVMClient`](../interfaces/SmolVMClient.md).[`diagnose`](../interfaces/SmolVMClient.md#diagnose)

[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / BrowserSessionClient

# Interface: BrowserSessionClient

Control a ready browser computer through private automation and viewing addresses. An endpoint is a local address used to connect to that computer.

## Extends

- [`ComputerClient`](ComputerClient.md)

## Properties

### cdpUrl

> `readonly` **cdpUrl**: `string`

***

### displayUrl?

> `readonly` `optional` **displayUrl?**: `string`

***

### files

> `readonly` **files**: [`SandboxFiles`](SandboxFiles.md)

#### Inherited from

[`ComputerClient`](ComputerClient.md).[`files`](ComputerClient.md#files)

***

### profileId?

> `readonly` `optional` **profileId?**: `string`

***

### sandboxId

> `readonly` **sandboxId**: `string`

***

### sessionId

> `readonly` **sessionId**: `string`

***

### status

> `readonly` **status**: [`BrowserSessionStatus`](../type-aliases/BrowserSessionStatus.md)

***

### viewerUrl?

> `readonly` `optional` **viewerUrl?**: `string`

## Methods

### delete()

> **delete**(): `Promise`\<`void`\>

#### Returns

`Promise`\<`void`\>

***

### exec()

> **exec**(`command`, `options?`): `Promise`\<[`ExecResult`](ExecResult.md)\>

#### Parameters

##### command

`string` \| readonly `string`[]

##### options?

[`ExecOptions`](ExecOptions.md)

#### Returns

`Promise`\<[`ExecResult`](ExecResult.md)\>

#### Inherited from

[`ComputerClient`](ComputerClient.md).[`exec`](ComputerClient.md#exec)

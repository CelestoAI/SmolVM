[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / BrowserSessionClient

# Interface: BrowserSessionClient

Control a ready browser computer through private automation and viewing addresses. An endpoint is a local address used to connect to that computer.

## Extends

- [`CommandFilesClient`](CommandFilesClient.md)

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

[`CommandFilesClient`](CommandFilesClient.md).[`files`](CommandFilesClient.md#files)

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

[`CommandFilesClient`](CommandFilesClient.md).[`exec`](CommandFilesClient.md#exec)

[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / BrowserSessionClient

# Interface: BrowserSessionClient

Control a ready browser computer through private automation and viewing addresses. An endpoint is a local address used to connect to that computer.

## Properties

### cdpUrl

> `readonly` **cdpUrl**: `string`

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

Run a command as the unprivileged agent user inside this browser VM.

#### Parameters

##### command

`string` \| readonly `string`[]

##### options?

[`ExecOptions`](ExecOptions.md)

#### Returns

`Promise`\<[`ExecResult`](ExecResult.md)\>

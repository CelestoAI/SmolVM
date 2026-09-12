[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SandboxClient

# Interface: SandboxClient

## Properties

### files

> `readonly` **files**: [`SandboxFiles`](SandboxFiles.md)

***

### id

> `readonly` **id**: `string`

***

### status

> `readonly` **status**: [`SandboxStatus`](../type-aliases/SandboxStatus.md)

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

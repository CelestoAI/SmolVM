[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / Sandbox

# Class: Sandbox

One disposable local computer with commands, files, status, and explicit deletion.

## Implements

- [`SandboxClient`](../interfaces/SandboxClient.md)

## Properties

### files

> `readonly` **files**: [`SandboxFiles`](../interfaces/SandboxFiles.md)

#### Implementation of

[`SandboxClient`](../interfaces/SandboxClient.md).[`files`](../interfaces/SandboxClient.md#files)

***

### id

> `readonly` **id**: `string`

#### Implementation of

[`SandboxClient`](../interfaces/SandboxClient.md).[`id`](../interfaces/SandboxClient.md#id)

## Accessors

### status

#### Get Signature

> **get** **status**(): [`SandboxStatus`](../type-aliases/SandboxStatus.md)

##### Returns

[`SandboxStatus`](../type-aliases/SandboxStatus.md)

#### Implementation of

[`SandboxClient`](../interfaces/SandboxClient.md).[`status`](../interfaces/SandboxClient.md#status)

## Methods

### delete()

> **delete**(): `Promise`\<`void`\>

#### Returns

`Promise`\<`void`\>

#### Implementation of

[`SandboxClient`](../interfaces/SandboxClient.md).[`delete`](../interfaces/SandboxClient.md#delete)

***

### exec()

> **exec**(`command`, `options?`): `Promise`\<[`ExecResult`](../interfaces/ExecResult.md)\>

#### Parameters

##### command

`string` \| readonly `string`[]

##### options?

[`ExecOptions`](../interfaces/ExecOptions.md) = `{}`

#### Returns

`Promise`\<[`ExecResult`](../interfaces/ExecResult.md)\>

#### Implementation of

[`SandboxClient`](../interfaces/SandboxClient.md).[`exec`](../interfaces/SandboxClient.md#exec)

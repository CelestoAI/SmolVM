[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / ComputerSessionClient

# Interface: ComputerSessionClient

A complete Linux desktop grouped by display, browser, files, and commands.

## Extends

- [`CommandFilesClient`](CommandFilesClient.md)

## Properties

### browser

> `readonly` **browser**: [`ComputerBrowserClient`](ComputerBrowserClient.md)

***

### capabilities

> `readonly` **capabilities**: readonly `string`[]

***

### computerId

> `readonly` **computerId**: `string`

***

### display

> `readonly` **display**: [`ComputerDisplayClient`](ComputerDisplayClient.md)

***

### files

> `readonly` **files**: [`SandboxFiles`](SandboxFiles.md)

#### Inherited from

[`CommandFilesClient`](CommandFilesClient.md).[`files`](CommandFilesClient.md#files)

***

### sandboxId

> `readonly` **sandboxId**: `string`

***

### status

> `readonly` **status**: [`ComputerSessionStatus`](../type-aliases/ComputerSessionStatus.md)

***

### template

> `readonly` **template**: `"linux-desktop"`

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

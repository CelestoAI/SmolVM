[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / CommandFilesClient

# Interface: CommandFilesClient

Run commands and exchange files with one disposable environment.

## Extended by

- [`SandboxClient`](SandboxClient.md)
- [`BrowserSessionClient`](BrowserSessionClient.md)
- [`ComputerSessionClient`](ComputerSessionClient.md)

## Properties

### files

> `readonly` **files**: [`SandboxFiles`](SandboxFiles.md)

## Methods

### exec()

> **exec**(`command`, `options?`): `Promise`\<[`ExecResult`](ExecResult.md)\>

#### Parameters

##### command

`string` \| readonly `string`[]

##### options?

[`ExecOptions`](ExecOptions.md)

#### Returns

`Promise`\<[`ExecResult`](ExecResult.md)\>

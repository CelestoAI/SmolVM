[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / ComputerClient

# Interface: ComputerClient

Run commands and exchange files with one disposable computer.

## Extended by

- [`SandboxClient`](SandboxClient.md)
- [`BrowserSessionClient`](BrowserSessionClient.md)

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

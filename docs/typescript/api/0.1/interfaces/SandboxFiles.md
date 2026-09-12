[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SandboxFiles

# Interface: SandboxFiles

## Methods

### download()

> **download**(`sandboxPath`, `localPath`): `Promise`\<`void`\>

Download to a temporary host file, then rename it atomically.

#### Parameters

##### sandboxPath

`string`

##### localPath

`string`

#### Returns

`Promise`\<`void`\>

***

### read()

> **read**(`path`): `Promise`\<`string`\>

Read a UTF-8 text file from an absolute sandbox path.

#### Parameters

##### path

`string`

#### Returns

`Promise`\<`string`\>

***

### upload()

> **upload**(`localPath`, `sandboxPath`): `Promise`\<`void`\>

Stream a host file into the sandbox.

#### Parameters

##### localPath

`string`

##### sandboxPath

`string`

#### Returns

`Promise`\<`void`\>

***

### write()

> **write**(`path`, `content`): `Promise`\<`void`\>

Write text or bytes to an absolute sandbox path.

#### Parameters

##### path

`string`

##### content

`string` \| `Uint8Array`\<`ArrayBufferLike`\>

#### Returns

`Promise`\<`void`\>

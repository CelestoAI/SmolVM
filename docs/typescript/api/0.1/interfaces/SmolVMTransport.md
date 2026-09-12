[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / SmolVMTransport

# Interface: SmolVMTransport

Sends private bridge requests; applications can implement it to test without a VM.

## Methods

### close()

> **close**(): `Promise`\<`void`\>

#### Returns

`Promise`\<`void`\>

***

### request()

> **request**\<`T`\>(`path`, `init?`): `Promise`\<`T`\>

#### Type Parameters

##### T

`T`

#### Parameters

##### path

`string`

##### init?

`RequestInit`

#### Returns

`Promise`\<`T`\>

***

### requestBytes()

> **requestBytes**(`path`, `init?`): `Promise`\<`Uint8Array`\<`ArrayBufferLike`\>\>

#### Parameters

##### path

`string`

##### init?

`RequestInit`

#### Returns

`Promise`\<`Uint8Array`\<`ArrayBufferLike`\>\>

***

### requestStream()?

> `optional` **requestStream**(`path`, `content`, `contentLength`): `Promise`\<`void`\>

#### Parameters

##### path

`string`

##### content

`AsyncIterable`\<`Uint8Array`\<`ArrayBufferLike`\>\>

##### contentLength

`number`

#### Returns

`Promise`\<`void`\>

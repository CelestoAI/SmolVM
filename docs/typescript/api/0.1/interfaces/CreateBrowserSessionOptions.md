[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / CreateBrowserSessionOptions

# Interface: CreateBrowserSessionOptions

Start an isolated browser computer with the resources and live view you need. Chromium is the browser engine running inside it.

## Properties

### allowDownloads?

> `optional` **allowDownloads?**: `boolean`

***

### backend?

> `optional` **backend?**: `"firecracker"` \| `"qemu"` \| `"libkrun"` \| `"auto"`

***

### diskMiB?

> `optional` **diskMiB?**: `number`

***

### memoryMiB?

> `optional` **memoryMiB?**: `number`

***

### mode?

> `optional` **mode?**: `"headless"` \| `"live"`

***

### network?

> `optional` **network?**: [`NetworkPolicy`](../type-aliases/NetworkPolicy.md)

***

### profile?

> `optional` **profile?**: \{ `mode`: `"ephemeral"`; \} \| \{ `id`: `string`; `mode`: `"persistent"`; \}

***

### recordVideo?

> `optional` **recordVideo?**: `boolean`

***

### sessionId?

> `optional` **sessionId?**: `string`

***

### timeoutMinutes?

> `optional` **timeoutMinutes?**: `number`

***

### viewport?

> `optional` **viewport?**: `object`

#### height

> **height**: `number`

#### width

> **width**: `number`

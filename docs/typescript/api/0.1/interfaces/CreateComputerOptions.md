[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / CreateComputerOptions

# Interface: CreateComputerOptions

Start a complete Linux desktop with a display, Chromium, files, and commands.

## Properties

### backend?

> `optional` **backend?**: `"firecracker"` \| `"qemu"` \| `"auto"`

***

### display?

> `optional` **display?**: `object`

#### height

> **height**: `number`

#### width

> **width**: `number`

***

### name?

> `optional` **name?**: `string`

***

### network?

> `optional` **network?**: [`NetworkPolicy`](../type-aliases/NetworkPolicy.md)

***

### resources?

> `optional` **resources?**: `object`

#### diskMiB?

> `optional` **diskMiB?**: `number`

#### memoryMiB?

> `optional` **memoryMiB?**: `number`

#### vcpus?

> `optional` **vcpus?**: `2`

***

### template?

> `optional` **template?**: `"linux-desktop"`

***

### workspace?

> `optional` **workspace?**: readonly `object`[]

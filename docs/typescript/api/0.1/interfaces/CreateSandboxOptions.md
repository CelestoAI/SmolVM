[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / CreateSandboxOptions

# Interface: CreateSandboxOptions

## Properties

### backend?

> `optional` **backend?**: `"firecracker"` \| `"qemu"` \| `"libkrun"` \| `"vz"`

Override automatic backend selection.

***

### diskMiB?

> `optional` **diskMiB?**: `number`

Guest root disk size in MiB.

***

### image?

> `optional` **image?**: `string`

Custom local path, file URL, or remote image reference.

***

### memoryMiB?

> `optional` **memoryMiB?**: `number`

Guest memory in MiB.

***

### network?

> `optional` **network?**: [`NetworkPolicy`](../type-aliases/NetworkPolicy.md)

Outbound network access. Defaults to open.

***

### os?

> `optional` **os?**: `"ubuntu"` \| `"alpine"`

Guest operating system. Defaults to Ubuntu.

[**@celestoai/smolvm**](../README.md)

***

[@celestoai/smolvm](../README.md) / NetworkPolicy

# Type Alias: NetworkPolicy

> **NetworkPolicy** = \{ `mode`: `"open"`; \} \| \{ `mode`: `"off"`; \} \| \{ `allowedCidrs`: readonly `string`[]; `mode`: `"restricted"`; \}

Controls which outbound IPv4 connections a new sandbox may make.

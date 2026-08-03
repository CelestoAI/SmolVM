import { Smolvm } from '../src/index'
import type { DesktopResponse } from '../src/index'

// Compile-time public-SDK contract: the HTTP desktop endpoint should be
// reachable through the documented Smolvm facade, and should hand back the
// generated DesktopResponse rather than a handwritten duplicate.
const smolvm = new Smolvm()

async function readDesktop(sandboxId: string): Promise<string> {
  const desktop: DesktopResponse = await smolvm.sandbox.desktop(sandboxId)

  // Each field is used at its generated type, so a schema change that drops
  // or retypes one of them fails this compile instead of silently reaching
  // callers.
  const protocol: 'vnc' | undefined = desktop.protocol
  const host: '127.0.0.1' | 'localhost' | '::1' = desktop.host
  const port: number = desktop.port
  const viewerUrl: string = desktop.viewer_url

  return `${protocol ?? 'vnc'}://${host}:${port} (${viewerUrl})`
}

void readDesktop('sandbox-id')

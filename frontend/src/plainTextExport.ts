export type PlainTextExport = {
  filename: string
  text: string
}

type PlainTextDownloadRuntime = {
  urlApi: Pick<typeof URL, 'createObjectURL' | 'revokeObjectURL'>
  BlobCtor: typeof Blob
  document: Document
  window: Window
}

type PlainTextCopyRuntime = {
  clipboard?: Pick<Clipboard, 'writeText'> | null
  document: Document
}

export async function copyPlainText(
  text: string,
  runtime: PlainTextCopyRuntime = { clipboard: navigator.clipboard, document },
) {
  if (runtime.clipboard?.writeText) {
    try {
      await runtime.clipboard.writeText(text)
      return
    } catch {
      // Fall back to the document selection when clipboard permission is unavailable.
    }
  }
  const input = runtime.document.createElement('textarea')
  input.value = text
  input.setAttribute('readonly', '')
  input.style.position = 'fixed'
  input.style.opacity = '0'
  runtime.document.body.appendChild(input)
  input.select()
  const copied = runtime.document.execCommand('copy')
  input.remove()
  if (!copied) throw new Error('复制失败，请检查浏览器剪贴板权限后重试。')
}

export function preparePlainTextExport(title: string, text: string): PlainTextExport {
  const safeTitle = title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').trim().slice(0, 80) || '平台文案'
  return { filename: `${safeTitle}.txt`, text }
}

export function downloadPlainText(
  title: string,
  text: string,
  runtime: PlainTextDownloadRuntime = { urlApi: URL, BlobCtor: Blob, document, window },
) {
  const prepared = preparePlainTextExport(title, text)
  const objectUrl = runtime.urlApi.createObjectURL(new runtime.BlobCtor([prepared.text], { type: 'text/plain;charset=utf-8' }))
  const link = runtime.document.createElement('a')
  link.href = objectUrl
  link.download = prepared.filename
  link.click()
  runtime.window.setTimeout(() => runtime.urlApi.revokeObjectURL(objectUrl), 0)
}

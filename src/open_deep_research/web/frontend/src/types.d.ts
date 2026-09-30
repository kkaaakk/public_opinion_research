/// <reference types="vite/client" />
declare module '@trajectory-upstream' {
  import type { ComponentType } from 'react'
  export const TrajectoryView: ComponentType<any>
}
declare module '@deepseek-ai/dsh-client-ui-primitives' {
  import type { ComponentType } from 'react'
  export const MarkdownText: ComponentType<{text: string; labels: unknown}>
}
declare module '@deepseek-ai/dsh-client-ui-slots' {
  export interface LocaleNamespaceMap {}
  export type TranslateNS<N extends string> = (key: string, values?: Record<string, string | number>) => string
}

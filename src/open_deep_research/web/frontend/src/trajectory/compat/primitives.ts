// Narrow import face for the original DSH trajectory package. Each export
// resolves to the corresponding vendored DSH primitive, with no app runtime.
export { CodeBlock } from '../upstream-primitives/markdown/CodeBlock.tsx'
export { MarkdownText } from '../upstream-primitives/markdown/MarkdownText.tsx'
export { extractMarkdownPlainText } from '../upstream-primitives/markdown/plain-text.ts'
export { JsonTree } from '../upstream-primitives/JsonTree.tsx'
export { Tooltip } from '../upstream-primitives/Tooltip.tsx'
export { StateDot } from '../upstream-primitives/StateDot.tsx'
export { FileTypeIcon, fileExtension } from '../upstream-primitives/FileTypeIcon.tsx'
export { fileSizeText } from '../upstream-primitives/file-size.ts'
export { writeClipboard } from '../upstream-primitives/clipboard.ts'
export {
  IconCheckOutlineRegular, IconChevronRightOutlineRegular,
  IconCodeOutlineRegular, IconWrapLinesOutlineRegular,
  IconCopyOutlineRegular, IconSettingsOutlineRegular,
  IconSparkleRegular, IconUserOutlineRegular,
  IconSearchOutlineRegular,
} from '../upstream-primitives/icons/index.tsx'
export type { JsonTreeProps, JsonTreeLabels } from '../upstream-primitives/JsonTree.tsx'
export type { MarkdownLabels } from '../upstream-primitives/markdown/MarkdownText.tsx'

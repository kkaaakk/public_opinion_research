import { Copy, Download, Printer } from 'lucide-react';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Button } from '@/components/ui/button';

const plugins = [remarkGfm];
const headings = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'] as const;

const markdownComponents: Components = {
  a({ href, children }) {
    const external = /^(https?:\/\/|mailto:)/i.test(href ?? '');
    return (
      <a
        href={href}
        target={external ? '_blank' : undefined}
        rel={external ? 'noreferrer noopener' : undefined}
      >
        {children}
      </a>
    );
  },
  img({ src, alt }) {
    return src ? (
      <a href={src} target="_blank" rel="noreferrer noopener">
        {alt || '查看原图'}
      </a>
    ) : (
      <>{alt}</>
    );
  },
  table({ children }) {
    return (
      <div className="markdown-table" tabIndex={0}>
        <table>{children}</table>
      </div>
    );
  },
};

function reportUrl(url: string) {
  return /^(https?:\/\/|mailto:|#)/i.test(url) ? defaultUrlTransform(url) : '';
}

export const Markdown = memo(function Markdown({
  body,
  anchor,
}: {
  body: string;
  anchor?: string;
}) {
  const components = useMemo<Components>(() => {
    if (!anchor) return markdownComponents;
    const result = { ...markdownComponents };
    for (const Heading of headings) {
      result[Heading] = ({ node, children }) => (
        <Heading
          id={node?.position?.start.line ? anchor + '-' + node.position.start.line : undefined}
        >
          {children}
        </Heading>
      );
    }
    return result;
  }, [anchor]);
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={plugins}
        urlTransform={reportUrl}
        components={components}
        skipHtml
      >
        {body}
      </ReactMarkdown>
    </div>
  );
});

export function Report({ body, topic }: { body: string; topic: string }) {
  const content = useRef<HTMLDivElement>(null);
  const [toc, setToc] = useState<{ id: string; name: string; level: number }[]>([]);
  const [copy, setCopy] = useState('');
  useEffect(() => {
    const nodes = content.current?.querySelectorAll<HTMLElement>('[id^="report-heading-"]');
    setToc(
      Array.from(nodes ?? [], (node) => ({
        id: node.id,
        name: node.textContent ?? '',
        level: Number(node.tagName.slice(1)),
      })),
    );
  }, [body]);

  async function copyReport() {
    try {
      await navigator.clipboard.writeText(body);
      setCopy('已复制 Markdown');
    } catch {
      setCopy('浏览器未授予剪贴板权限。');
    }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([body], { type: 'text/markdown;charset=utf-8' }));
    const filename = Array.from(topic.trim())
      .slice(0, 72)
      .join('')
      .replace(/[^\p{L}\p{N}_.() -]/gu, '_');
    const link = document.createElement('a');
    link.href = url;
    link.download = (filename || '研究报告') + '.md';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return (
    <article className="final-report" id="final-report">
      <header className="report-toolbar">
        <h2>最终报告</h2>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => void copyReport()}>
            <Copy data-icon="inline-start" />
            复制 Markdown
          </Button>
          <Button variant="outline" onClick={download}>
            <Download data-icon="inline-start" />
            下载
          </Button>
          <Button variant="outline" onClick={() => window.print()}>
            <Printer data-icon="inline-start" />
            打印 / PDF
          </Button>
        </div>
        {copy && (
          <p className="muted w-full" role="status">
            {copy}
          </p>
        )}
      </header>
      {toc.length > 1 && (
        <details className="report-toc">
          <summary>报告目录</summary>
          <nav aria-label="报告目录">
            {toc.map(({ id, name, level }) => (
              <a
                key={id}
                href={'#' + id}
                onClick={(event) => {
                  event.preventDefault();
                  document.getElementById(id)?.scrollIntoView({ block: 'start' });
                }}
                style={{ paddingInlineStart: Math.max(0, level - 2) * 12 }}
              >
                {name}
              </a>
            ))}
          </nav>
        </details>
      )}
      <div className="report-body" ref={content}>
        <Markdown body={body} anchor="report-heading" />
      </div>
    </article>
  );
}

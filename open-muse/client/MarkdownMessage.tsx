import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface MarkdownMessageProps {
  children: string;
}

export function MarkdownMessage({ children }: MarkdownMessageProps) {
  return <div className="markdown-message">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      components={{
        a: ({ children: linkText, node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer">{linkText}</a>,
      }}
    >
      {children}
    </ReactMarkdown>
  </div>;
}

import Prism from "prismjs";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-csharp";
import "prismjs/components/prism-css";
import "prismjs/components/prism-docker";
import "prismjs/components/prism-json";
import "prismjs/components/prism-jsx";
import "prismjs/components/prism-markdown";
import "prismjs/components/prism-powershell";
import "prismjs/components/prism-python";
import "prismjs/components/prism-sql";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-tsx";
import "prismjs/components/prism-yaml";
import type { ReactNode } from "react";

function renderToken(token: string | Prism.Token, key: string): ReactNode {
  if (typeof token === "string") return token;
  const content = Array.isArray(token.content)
    ? token.content.map((child, index) =>
        renderToken(child, `${key}-${index}`),
      )
    : renderToken(token.content, `${key}-content`);
  return (
    <span className={`token ${token.type}`} key={key}>
      {content}
    </span>
  );
}

export function SyntaxHighlightedCode({
  content,
  language,
}: {
  content: string;
  language: string | null;
}) {
  const selected = language ?? "text";
  const grammar = Prism.languages[selected] ?? Prism.languages.plain;
  const tokens = Prism.tokenize(content, grammar);
  return (
    <pre className={`project-code language-${selected}`} tabIndex={0}>
      <code>
        {tokens.map((token, index) => renderToken(token, String(index)))}
      </code>
    </pre>
  );
}

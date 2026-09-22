import { defineConfig } from "vitepress";

export default defineConfig({
  title: "Open Deep Research",
  description: "Open Deep Research project documentation",
  cleanUrls: true,
  lastUpdated: true,
  themeConfig: {
    nav: [
      { text: "首页", link: "/" },
      { text: "快速开始", link: "/guide/project-readme" },
      { text: "架构", link: "/architecture/technical-stack" },
      { text: "RAG", link: "/architecture/rag" },
      { text: "评估", link: "/evaluation/rag-retrieval-test-records" },
      { text: "参考", link: "/project/agents" },
    ],
    sidebar: {
      "/": [
        {
          text: "开始",
          items: [
            { text: "文档首页", link: "/" },
            { text: "项目 README", link: "/guide/project-readme" },
          ],
        },
        {
          text: "架构与模块",
          items: [
            { text: "当前技术栈", link: "/architecture/technical-stack" },
            { text: "Agent Loop", link: "/architecture/agent-loop" },
            { text: "Tools 模块", link: "/architecture/tools" },
            { text: "RAG 模块", link: "/architecture/rag" },
            { text: "Memory 模块", link: "/architecture/memory" },
            { text: "RAG / Memory 笔记", link: "/architecture/rag-notes" },
          ],
        },
        {
          text: "评估与样例",
          items: [
            { text: "RAG 检索测试记录", link: "/evaluation/rag-retrieval-test-records" },
            { text: "ArXiv 示例", link: "/examples/arxiv" },
            { text: "PubMed 示例", link: "/examples/pubmed" },
            { text: "Inference Market 示例", link: "/examples/inference-market" },
            { text: "Inference Market GPT-4.5 示例", link: "/examples/inference-market-gpt45" },
          ],
        },
        {
          text: "知识库样例",
          items: [
            { text: "Team Handbook", link: "/knowledge/team-handbook" },
            { text: "Data Governance", link: "/knowledge/data-governance" },
            { text: "GitHub Testsets", link: "/knowledge/github-testsets" },
            { text: "Misleading Archive", link: "/knowledge/misleading-archive" },
          ],
        },
        {
          text: "项目参考",
          items: [
            { text: "Agent Instructions", link: "/project/agents" },
            { text: "Claude Instructions", link: "/project/claude" },
          ],
        },
      ],
    },
    search: {
      provider: "local",
    },
    outline: {
      level: [2, 3],
      label: "本页目录",
    },
    docFooter: {
      prev: "上一页",
      next: "下一页",
    },
    lastUpdated: {
      text: "最后更新",
      formatOptions: {
        dateStyle: "medium",
        timeStyle: "short",
      },
    },
    socialLinks: [
      { icon: "github", link: "https://github.com/langchain-ai/open_deep_research" },
    ],
  },
});

import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

const repo = 'https://github.com/BijectiveLabs/trading-research-agents'

export default withMermaid(
  defineConfig({
    lang: 'en-US',
    title: 'Bijective',
    titleTemplate: ':title | Bijective Docs',
    description: 'Documentation for trading-research-agents, by Bijective Labs.',
    appearance: 'dark',
    cleanUrls: true,
    lastUpdated: true,
    srcExclude: ['**/node_modules/**'],
    head: [
      ['link', { rel: 'icon', href: '/icon.svg' }],
      ['link', { rel: 'preconnect', href: 'https://fonts.googleapis.com' }],
      ['link', { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' }],
      ['link', { rel: 'stylesheet', href: 'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Montserrat:wght@100;300;400&display=swap' }],
      ['link', { rel: 'stylesheet', href: 'https://fonts.googleapis.com/css2?family=Rubik:wght@400&display=swap' }],
      // Keeps the page background from flashing white during overscroll before the theme loads.
      ['style', {}, 'html, body { background: var(--docs-bg, #f9f9f9) !important; min-height: 100dvh !important; overscroll-behavior: none !important; overflow-x: clip !important; }']
    ],
    themeConfig: {
      logo: {
        light: '/bijective_docs_logo_black.svg',
        dark: '/bijective_docs_logo.svg',
        alt: 'BIJECTIVE'
      },
      siteTitle: false,
      search: { provider: 'local' },
      socialLinks: [{ icon: 'github', link: repo }],
      nav: [
        { text: 'Open Source', link: '/opensource/' },
        { text: 'Bijective Labs', link: 'https://bijectivelabs.dev' }
      ],
      sidebar: {
        '/opensource/': [
          {
            text: 'Start',
            items: [
              { text: 'Introduction', link: '/opensource/' },
              { text: 'Quick start', link: '/opensource/quick-start' },
              { text: 'What you see', link: '/opensource/what-you-see' }
            ]
          },
          {
            text: 'How it works',
            items: [
              { text: 'The cycle', link: '/opensource/cycle' },
              { text: 'Agents', link: '/opensource/agents' },
              { text: 'Data sources', link: '/opensource/sources' },
              { text: 'Capital, orders and positions', link: '/opensource/capital' },
              { text: 'Venues', link: '/opensource/venues' }
            ]
          },
          {
            text: 'Models',
            items: [
              { text: 'Working with language models', link: '/opensource/llm' },
              { text: 'Model providers', link: '/opensource/providers' }
            ]
          },
          {
            text: 'Research and testing',
            items: [
              { text: 'The data gate', link: '/opensource/data' },
              { text: 'The trading agent', link: '/opensource/trading' },
              { text: 'The live tier', link: '/opensource/live' },
              { text: 'The falsification chain', link: '/opensource/chain' },
              { text: 'The kill ledger', link: '/opensource/ledger' },
              { text: 'Execution realism', link: '/opensource/execution' },
              { text: 'The agent', link: '/opensource/agent' },
              { text: 'The MCP server', link: '/opensource/mcp' }
            ]
          },
          {
            text: 'Reference',
            items: [
              { text: 'Command line', link: '/opensource/commands' },
              { text: 'Architecture', link: '/opensource/architecture' },
              { text: 'Boundaries', link: '/opensource/boundaries' },
              { text: 'Liability', link: '/opensource/liability' },
              { text: 'Security', link: '/opensource/security' },
              { text: 'Contributing', link: '/opensource/contributing' },
              { text: 'Changelog', link: `${repo}/blob/develop/CHANGELOG.md` }
            ]
          }
        ]
      },
      editLink: {
        pattern: `${repo}/edit/develop/docs/:path`,
        text: 'Edit this page on GitHub'
      },
      outline: {
        level: [2, 3],
        label: 'On this page'
      },
      aside: true,
      docFooter: {
        prev: 'Previous',
        next: 'Next'
      },
      lastUpdatedText: 'Last updated',
      returnToTopLabel: 'Return to top',
      footer: {
        message: 'Released under the MIT License.',
        copyright: 'Copyright 2026 Bijective Labs'
      }
    }
  })
)

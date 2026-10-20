import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { defineConfig, type HeadConfig, type SiteConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

const repo = 'https://github.com/BijectiveLabs/trading-research-agents'
const host = 'https://docs.bijectivelabs.dev'
const pypi = 'https://pypi.org/project/trading-research-agents/'
const website = 'https://www.bijectivelabs.dev/open-source'
const description =
  'Documentation for trading-research-agents, open-source autonomous trading agents with deterministic ' +
  'execution by ' +
  'Bijective Labs: install, the decision cycle, agents, model providers, venues and controls.'
const summary =
  'An open-source Python package and command line (tra): autonomous trading agents with ' +
  'deterministic execution. ' +
  'Language model agents read the market and propose a trade; fixed, tested rules compute every ' +
  'figure, price the round trip, size the position and decide whether an order goes out. It runs ' +
  'in a sandbox by default, trades linear perpetuals today on any exchange ccxt supports, with more ' +
  'market types on the way, and works with Anthropic, OpenAI, ' +
  'Google Gemini, Groq, DeepSeek, OpenRouter, Ollama and other model providers. MIT licensed.'
const socialImage = { url: `${host}/images/console.png`, width: '1199', height: '630' }

function packageVersion(): string | undefined {
  try {
    const toml = readFileSync(new URL('../../pyproject.toml', import.meta.url), 'utf-8')
    return toml.match(/^version = "([^"]+)"/m)?.[1]
  } catch {
    return undefined
  }
}

const version = packageVersion()

// 'opensource/cycle.md' -> 'opensource/cycle', 'opensource/index.md' -> 'opensource/'
function pagePath(relativePath: string): string {
  return relativePath.replace(/(^|\/)index\.md$/, '$1').replace(/\.md$/, '')
}

const sidebar = [
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
      { text: 'Your own data', link: '/opensource/your-data' },
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
      { text: 'Changelog', link: `${repo}/blob/master/CHANGELOG.md` }
    ]
  }
]

function structuredData(): HeadConfig {
  const data = {
    '@context': 'https://schema.org',
    '@type': 'SoftwareSourceCode',
    name: 'trading-research-agents',
    description: summary,
    url: website,
    codeRepository: repo,
    programmingLanguage: 'Python',
    runtimePlatform: 'Python 3.11+',
    license: 'https://opensource.org/licenses/MIT',
    ...(version ? { version } : {}),
    sameAs: [repo, pypi, `${host}/opensource/`],
    publisher: { '@type': 'Organization', name: 'Bijective Labs', url: 'https://www.bijectivelabs.dev' }
  }
  return ['script', { type: 'application/ld+json' }, JSON.stringify(data)]
}

// Plain Markdown beside every page, plus llms.txt and llms-full.txt, so a crawler or a language
// model reads the docs as text instead of rendering the site.
function writeTextCopies(siteConfig: SiteConfig) {
  const pages: { url: string; title: string; description: string; body: string }[] = []
  for (const group of sidebar) {
    for (const item of group.items) {
      if (!item.link.startsWith('/')) continue
      const rel = item.link.endsWith('/') ? `${item.link.slice(1)}index.md` : `${item.link.slice(1)}.md`
      const src = join(siteConfig.srcDir, rel)
      if (!existsSync(src)) continue
      const raw = readFileSync(src, 'utf-8').replace(/\r\n/g, '\n')
      const fm = raw.match(/^---\n([\s\S]*?)\n---\n/)
      const body = (fm ? raw.slice(fm[0].length) : raw)
        .replace(/\]\(\/(?!\/)/g, `](${host}/`)
        .trim()
      const desc = fm?.[1].match(/^description:\s*"?(.*?)"?\s*$/m)?.[1] ?? ''
      const out = join(siteConfig.outDir, rel)
      mkdirSync(dirname(out), { recursive: true })
      writeFileSync(out, body + '\n')
      pages.push({ url: `${host}/${rel}`, title: item.text, description: desc, body })
    }
  }

  const heading = `# trading-research-agents\n\n> ${summary}\n`
  const facts = [
    version ? `Current release: ${version}.` : '',
    "Install: uv tool install 'trading-research-agents[ccxt,openai,anthropic,google,data]'",
    'Command: tra. Python 3.11 or newer.',
    `Website: ${website}`,
    `Source: ${repo}`,
    `Package: ${pypi}`
  ].filter(Boolean)
  const index = [
    heading,
    facts.join('\n'),
    '',
    ...sidebar.flatMap((group) => {
      const rows = group.items
        .map((item) => {
          const page = pages.find((p) => p.title === item.text)
          if (page) return `- [${page.title}](${page.url})${page.description ? `: ${page.description}` : ''}`
          return item.link.startsWith('http') ? `- [${item.text}](${item.link})` : ''
        })
        .filter(Boolean)
      return [`## ${group.text}`, '', ...rows, '']
    }),
    '## Optional',
    '',
    `- [Full documentation as one file](${host}/llms-full.txt)`,
    ''
  ].join('\n')
  writeFileSync(join(siteConfig.outDir, 'llms.txt'), index)

  const full = [heading, facts.join('\n'), '', ...pages.map((p) => `<!-- ${p.url} -->\n\n${p.body}\n`)].join('\n')
  writeFileSync(join(siteConfig.outDir, 'llms-full.txt'), full)
}

export default withMermaid(
  defineConfig({
    lang: 'en-US',
    title: 'Bijective',
    titleTemplate: ':title | Bijective Docs',
    description,
    appearance: 'dark',
    cleanUrls: true,
    lastUpdated: true,
    srcExclude: ['**/node_modules/**'],
    sitemap: { hostname: host },
    head: [
      ['link', { rel: 'icon', href: '/icon.svg' }],
      ['link', { rel: 'preconnect', href: 'https://fonts.googleapis.com' }],
      ['link', { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' }],
      ['link', { rel: 'stylesheet', href: 'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Montserrat:wght@100;300;400&display=swap' }],
      ['link', { rel: 'stylesheet', href: 'https://fonts.googleapis.com/css2?family=Rubik:wght@400&display=swap' }],
      ['meta', { property: 'og:site_name', content: 'Bijective Docs' }],
      ['meta', { property: 'og:type', content: 'website' }],
      ['meta', { property: 'og:image', content: socialImage.url }],
      ['meta', { property: 'og:image:width', content: socialImage.width }],
      ['meta', { property: 'og:image:height', content: socialImage.height }],
      ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
      ['meta', { name: 'twitter:image', content: socialImage.url }],
      // Keeps the page background from flashing white during overscroll before the theme loads.
      ['style', {}, 'html, body { background: var(--docs-bg, #f9f9f9) !important; min-height: 100dvh !important; overscroll-behavior: none !important; overflow-x: clip !important; }']
    ],
    transformPageData(pageData) {
      if (pageData.relativePath.startsWith('opensource/') && pageData.titleTemplate === undefined) {
        pageData.titleTemplate = ':title | trading-research-agents'
      }
    },
    transformHead({ pageData, siteData, title }) {
      const path = pagePath(pageData.relativePath)
      const url = `${host}/${path}`
      const pageDescription = pageData.description || siteData.description
      const head: HeadConfig[] = [
        ['link', { rel: 'canonical', href: url }],
        ['meta', { property: 'og:url', content: url }],
        ['meta', { property: 'og:title', content: title }],
        ['meta', { property: 'og:description', content: pageDescription }],
        ['meta', { name: 'twitter:title', content: title }],
        ['meta', { name: 'twitter:description', content: pageDescription }]
      ]
      if (path.startsWith('opensource')) {
        const md = path.endsWith('/') ? `${path}index.md` : `${path}.md`
        head.push(['link', { rel: 'alternate', type: 'text/markdown', href: `${host}/${md}` }])
      }
      if (path === '' || path === 'opensource/') head.push(structuredData())
      return head
    },
    buildEnd: writeTextCopies,
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
        { text: 'Bijective Labs', link: 'https://www.bijectivelabs.dev' }
      ],
      sidebar: { '/opensource/': sidebar },
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

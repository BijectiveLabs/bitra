// Brings the Bijective platform's documentation into this site under /bijective/.
//
// The platform repository is the source: its docs/ tree is copied as written, a link that leaves
// docs/ is pointed at the file on GitHub, and the sidebar is read from the section index pages.
// Usage: node docs/.vitepress/platform.mjs <platform-checkout> <site-docs-dir>
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, posix, relative, resolve } from 'node:path'

const REPO = 'https://github.com/BijectiveLabs/bijective'
const RAW = 'https://raw.githubusercontent.com/BijectiveLabs/bijective'
const BRANCH = 'master'

const [checkout, siteDocs] = process.argv.slice(2)
if (!checkout || !siteDocs) {
  console.error('usage: node platform.mjs <platform-checkout> <site-docs-dir>')
  process.exit(2)
}
const source = resolve(checkout, 'docs')
const target = resolve(siteDocs, 'bijective')
if (!existsSync(join(source, 'index.md'))) {
  console.error(`${source} holds no platform documentation`)
  process.exit(1)
}

function markdownFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return markdownFiles(path)
    return name.endsWith('.md') ? [path] : []
  })
}

// A link inside docs/ stays relative. One that leaves it, or names a directory, goes to GitHub.
function rewrite(text, file) {
  return text.replace(/\]\(([^)\s]+)\)/g, (whole, link) => {
    if (/^[a-z]+:|^#|^\//i.test(link)) return whole
    const [path, anchor] = link.split('#')
    const absolute = resolve(dirname(file), path)
    const inDocs = absolute.startsWith(source + '/') || absolute === source
    const exists = existsSync(absolute)
    if (inDocs && exists && !statSync(absolute).isDirectory() && absolute.endsWith('.md')) return whole
    const inRepo = relative(resolve(checkout), absolute).split('\\').join('/')
    if (/\.(png|svg|jpe?g|gif|webp)$/i.test(inRepo)) {
      return `](${RAW}/${BRANCH}/${inRepo})`
    }
    const kind = exists && statSync(absolute).isDirectory() ? 'tree' : 'blob'
    return `](${REPO}/${kind}/${BRANCH}/${inRepo}${anchor ? `#${anchor}` : ''})`
  })
}

// A code span continued onto the next line is joined to one line. The site compiles every page as
// a Vue template, which reads a placeholder such as <dir> at the start of a line as a tag.
function joinSpans(text) {
  const lines = text.split('\n')
  const out = []
  let fenced = false
  for (let i = 0; i < lines.length; i += 1) {
    let line = lines[i]
    if (line.trimStart().startsWith('```')) {
      fenced = !fenced
      out.push(line)
      continue
    }
    while (!fenced && (line.match(/`/g) || []).length % 2 === 1 && i + 1 < lines.length && lines[i + 1].trim()) {
      i += 1
      line = `${line.trimEnd()} ${lines[i].trimStart()}`
    }
    out.push(line)
  }
  return out.join('\n')
}

// Sidebar groups are the sections the platform's docs/index.md lists, and each group's items are
// the pages that section's index lists, in the platform's own order.
function sidebar() {
  const links = (file) =>
    [...readFileSync(file, 'utf-8').matchAll(/\[([^\]]+)\]\(([^)#\s]+\.md)\)/g)].map((m) => ({
      text: m[1],
      path: posix.normalize(m[2])
    }))
  const groups = [{ text: 'Bijective platform', items: [{ text: 'Overview', link: '/bijective/' }] }]
  for (const section of links(join(source, 'index.md'))) {
    const index = join(source, section.path)
    if (!existsSync(index)) continue
    const base = posix.dirname(section.path)
    const items = [{ text: 'Introduction', link: `/bijective/${base}/` }]
    for (const page of links(index)) {
      const path = posix.normalize(posix.join(base, page.path))
      if (!path.startsWith('..') && existsSync(join(source, path))) {
        items.push({ text: page.text, link: `/bijective/${path.replace(/\.md$/, '').replace(/(^|\/)index$/, '$1')}` })
      }
    }
    groups.push({ text: section.text, collapsed: true, items })
  }
  return groups
}

rmSync(target, { recursive: true, force: true })
mkdirSync(target, { recursive: true })
let pages = 0
for (const file of markdownFiles(source)) {
  const out = join(target, relative(source, file))
  mkdirSync(dirname(out), { recursive: true })
  writeFileSync(out, rewrite(joinSpans(readFileSync(file, 'utf-8')), file))
  pages += 1
}
writeFileSync(join(siteDocs, '.vitepress', 'platform-sidebar.json'), JSON.stringify(sidebar(), null, 2))
console.log(`platform documentation: ${pages} page(s) from ${source}`)

// Parses .vue files and compiles their <template> with Zammad's own Vue.
// usage: ZAMMAD_ROOT=/opt/zammad node validate_vue.cjs FILE...
const fs = require('fs')
const root = process.env.ZAMMAD_ROOT || process.cwd()
const sfc = require(require.resolve('vue/compiler-sfc', { paths: [root] }))

let bad = 0
for (const file of process.argv.slice(2)) {
  const src = fs.readFileSync(file, 'utf8')
  const { descriptor, errors } = sfc.parse(src, { filename: file })
  const problems = [...errors]
  if (!descriptor.template || !descriptor.template.content.trim()) {
    problems.push(new Error('template is missing or empty'))
  } else {
    const r = sfc.compileTemplate({ source: descriptor.template.content, filename: file, id: 'branding-check' })
    problems.push(...r.errors)
  }
  if (problems.length) {
    bad += 1
    console.log(`INVALID ${file}: ${problems.map((e) => e.message || String(e)).join('; ')}`)
  }
}
process.exit(bad ? 1 : 0)

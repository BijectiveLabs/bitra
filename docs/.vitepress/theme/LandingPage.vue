<template>
  <div class="landing-page" :class="{ dark: isDark }">
    <main class="landing-main" role="main">
      <section class="landing-hero">
        <div class="landing-hero-content">
          <h1 class="landing-title">Bijective Documentation</h1>
          <p class="landing-subtitle">Select your entry point</p>
        </div>

        <div class="landing-cards">
          <!-- Open Source Card: a real link, so crawlers and screen readers follow it -->
          <a class="landing-card" href="/opensource/" aria-label="Open Source documentation">
            <div class="card-icon" aria-hidden="true">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M6 9 12 15 18 9"/>
                <path d="M12 3v12"/>
                <rect x="2" y="7" width="20" height="14" rx="2"/>
              </svg>
            </div>
            <h2 class="card-title">Open Source</h2>
            <p class="card-description">Autonomous trading agents for crypto market</p>
            <span class="card-cta">
              <span>Enter Open Source</span>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M5 12h14M12 5l7 7-7 7"/>
              </svg>
            </span>
          </a>

          <!-- The Platform Card -->
          <article 
            class="landing-card disabled-card" 
            tabindex="-1" 
            role="button" 
            aria-label="The Platform documentation (coming soon)"
            aria-disabled="true"
            @click.stop.prevent=""
            @keydown.enter.stop.prevent=""
            @keydown.space.prevent=""
          >
            <div class="card-icon" aria-hidden="true">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5"/>
                <line x1="12" y1="22" x2="12" y2="15.5"/>
                <line x1="22" y1="8.5" x2="12" y2="15.5"/>
                <line x1="2" y1="8.5" x2="12" y2="15.5"/>
              </svg>
            </div>
            <h2 class="card-title">The Platform</h2>
            <p class="card-description"></p>
            <button class="card-cta" aria-label="Enter The Platform">
              <span>Coming Soon</span>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M5 12h14M12 5l7 7-7 7"/>
              </svg>
            </button>
          </article>
        </div>
      </section>

      <section class="landing-about" aria-labelledby="landing-about-title">
        <h2 id="landing-about-title" class="about-title">trading-research-agents</h2>
        <p class="about-text">
          An open-source Python package and command line (<code>tra</code>) for autonomous crypto
          trading agents. Language models read the market and propose a trade; fixed, tested rules
          compute every figure, price the round trip, size the position and decide whether an order
          goes out. Runs in a sandbox by default, trades on any exchange ccxt supports, and works
          with Anthropic, OpenAI, Google Gemini, Groq, DeepSeek, OpenRouter, Ollama and other model
          providers. MIT licensed, published by Bijective Labs.
        </p>
        <nav class="about-links" aria-label="Start here">
          <a href="/opensource/quick-start">Quick start</a>
          <a href="/opensource/cycle">The cycle</a>
          <a href="/opensource/agents">Agents</a>
          <a href="/opensource/providers">Model providers</a>
          <a href="/opensource/venues">Venues</a>
          <a href="/opensource/commands">Command line</a>
          <a href="https://www.bijectivelabs.dev/open-source">Website</a>
          <a href="https://github.com/BijectiveLabs/trading-research-agents">GitHub</a>
          <a href="https://pypi.org/project/trading-research-agents/">PyPI</a>
        </nav>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'

const isDark = ref(false)
let themeObserver: MutationObserver | null = null

const updateTheme = () => {
  if (typeof document !== 'undefined') {
    isDark.value = document.documentElement.classList.contains('dark')
  }
}

onMounted(() => {
  updateTheme()
  if (typeof document !== 'undefined') {
    themeObserver = new MutationObserver(updateTheme)
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  }
})

onBeforeUnmount(() => {
  if (themeObserver) themeObserver.disconnect()
})
</script>

<style scoped>
/* ===== Light theme (default) ===== */
.landing-page {
  --bg-card: #ffffff;
  --border-color: #b9bdc4;
  --text-primary: #1d2a3d;
  --text-secondary: #6a7689;
  --accent: #1f6feb;
  --card-shadow: 0 1px 3px rgba(29, 42, 61, 0.08), 0 4px 12px rgba(29, 42, 61, 0.05);
  --card-shadow-hover: 0 4px 16px rgba(29, 42, 61, 0.1), 0 8px 24px rgba(29, 42, 61, 0.08);
  --radius: 0;

  min-height: 100dvh;
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

/* ===== Dark theme (VitePress .dark class on html) ===== */
.dark .landing-page {
  --bg-card: #253347;
  --border-color: #3a4a5e;
  --text-primary: #f0f2f5;
  --text-secondary: #a0aab9;
  --accent: #4d8fff;
  --card-shadow: 0 1px 3px rgba(0, 0, 0, 0.3), 0 4px 12px rgba(0, 0, 0, 0.2);
  --card-shadow-hover: 0 4px 16px rgba(0, 0, 0, 0.4), 0 8px 24px rgba(0, 0, 0, 0.3);
}

/* ===== Main ===== */
.landing-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  max-width: 960px;
  width: 100%;
  margin: 0 auto;
  padding: 80px 24px;
  box-sizing: border-box;
}

/* ===== Hero ===== */
.landing-hero {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  width: 100%;
}

.landing-hero-content {
  margin-bottom: 56px;
}

.landing-title {
  margin: 0 0 12px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: clamp(2rem, 5vw, 3.5rem);
  font-weight: 700;
  line-height: 1.1;
  color: var(--text-primary);
  letter-spacing: -0.02em;
}

.landing-subtitle {
  margin: 0;
  font-size: clamp(1rem, 2vw, 1.25rem);
  font-weight: 400;
  color: var(--text-secondary);
  line-height: 1.5;
}

/* ===== Cards Grid ===== */
.landing-cards {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 24px;
  width: 100%;
}

/* ===== Card ===== */
.landing-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--card-shadow);
  padding: 40px 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  height: 100%;
  min-height: 360px;
  color: inherit;
  text-decoration: none;
  cursor: pointer;
  transition: 
    transform 200ms ease,
    box-shadow 200ms ease,
    border-color 200ms ease;
}

.landing-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--card-shadow-hover);
  border-color: var(--accent);
}

.landing-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.card-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 64px;
  height: 64px;
  background: rgba(31, 111, 235, 0.08);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  color: var(--accent);
  margin-bottom: 24px;
  flex-shrink: 0;
}

.card-title {
  margin: 0 0 12px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.2;
}

.card-description {
  margin: 0 0 32px;
  font-size: 1rem;
  color: var(--text-secondary);
  line-height: 1.6;
  max-width: 280px;
}

.card-cta {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 16px 32px;
  background: var(--text-primary);
  color: var(--bg-card);
  border: none;
  border-radius: var(--radius);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.8125rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  cursor: pointer;
  transition: 
    background 150ms ease,
    transform 100ms ease;
  margin-top: auto;
}

.landing-card:hover .card-cta {
  background: var(--accent);
}

.card-cta:active {
  transform: scale(0.98);
}

.card-cta svg {
  flex-shrink: 0;
  transition: transform 150ms ease;
}

.landing-card:hover .card-cta svg {
  transform: translateX(3px);
}

/* ===== About ===== */
.landing-about {
  margin: 72px auto 0;
  max-width: 720px;
  text-align: center;
}

.about-title {
  margin: 0 0 16px;
  padding: 0;
  border: none;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--text-primary);
}

.about-text {
  margin: 0 0 24px;
  font-size: 0.9375rem;
  line-height: 1.7;
  color: var(--text-secondary);
}

.about-text code {
  font-size: 0.875em;
}

.about-links {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px 20px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.8125rem;
}

.about-links a {
  color: var(--text-primary);
  text-decoration: none;
  border-bottom: 1px solid var(--border-color);
  padding-bottom: 2px;
}

.about-links a:hover {
  color: var(--accent);
  border-color: var(--accent);
}

/* ===== Responsive ===== */
@media (max-width: 900px) {
  .landing-cards {
    grid-template-columns: 1fr;
    max-width: 400px;
  }

  .landing-main {
    padding: 60px 20px;
  }

  .landing-hero-content {
    margin-bottom: 40px;
  }

  .landing-card {
    min-height: auto;
    padding: 32px 24px;
  }
}

@media (max-width: 600px) {
  .landing-main {
    padding: 48px 16px;
  }

  .landing-title {
    font-size: 1.75rem;
  }

  .landing-subtitle {
    font-size: 1rem;
  }

  .landing-cards {
    gap: 16px;
  }

  .landing-card {
    padding: 28px 20px;
  }

  .card-icon {
    width: 56px;
    height: 56px;
  }

  .card-title {
    font-size: 1.25rem;
  }

  .card-description {
    font-size: 0.9375rem;
  }

  .card-cta {
    padding: 14px 24px;
    font-size: 0.75rem;
  }
}

/* ===== Reduced Motion ===== */
@media (prefers-reduced-motion: reduce) {
  .landing-card,
  .card-cta,
  .card-cta svg {
    transition: none !important;
  }

  .landing-card:hover {
    transform: none;
  }
}
</style>
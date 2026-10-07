import DefaultTheme from 'vitepress/theme'
// @ts-ignore: Vite handles CSS imports; provide a local declaration to satisfy TS if available
import './styles.css'
import LandingPage from './LandingPage.vue'

export default {
	...DefaultTheme,
	enhanceApp(ctx: any) {
		if (DefaultTheme.enhanceApp) {
			DefaultTheme.enhanceApp(ctx)
		}
		ctx.app.component('LandingPage', LandingPage)
			// The custom JS overscroll guard has been removed in favor of CSS overscroll-behavior: none !important.
			// Add image zoom overlay behaviour for docs images
			if (typeof window !== 'undefined') {
				const setupImageZoom = () => {
					if (document.querySelector('.vp-image-zoom-overlay')) return
					const overlay = document.createElement('div')
					overlay.className = 'vp-image-zoom-overlay'
										overlay.innerHTML = `
											<div class="vp-image-zoom-content">
												<img class="vp-image-zoom-img" src="" alt="" />
												<button class="vp-image-zoom-close" aria-label="Close image">✕</button>
											</div>
										`
					document.body.appendChild(overlay)

					const imgEl = overlay.querySelector<HTMLImageElement>('.vp-image-zoom-img')!
					const closeBtn = overlay.querySelector<HTMLButtonElement>('.vp-image-zoom-close')!

					let loadFallbackTimer: number | null = null

					const openOverlay = (src: string, alt = '') => {
						if (!src) return
						// prepare state
						imgEl.alt = alt
						overlay.classList.add('loading')
						// clear any previous handlers/timers
						imgEl.onload = null
						if (loadFallbackTimer) { window.clearTimeout(loadFallbackTimer); loadFallbackTimer = null }

						// set source then wait for it to be ready before showing to ensure animation runs reliably on mobile
						imgEl.src = src

						const show = () => {
							if (loadFallbackTimer) { window.clearTimeout(loadFallbackTimer); loadFallbackTimer = null }
							overlay.classList.remove('loading')
							overlay.classList.add('open')
							document.documentElement.style.overflow = 'hidden'
						}

						if (imgEl.complete) {
							// already cached / ready
							show()
						} else if ((imgEl as any).decode) {
							;(imgEl as any).decode().then(show).catch(() => { imgEl.onload = show })
						} else {
							imgEl.onload = show
						}

						// fail-safe: if load takes too long, still open after 4s (avoids blocking on flaky connections)
						loadFallbackTimer = window.setTimeout(() => {
							overlay.classList.remove('loading')
							overlay.classList.add('open')
							document.documentElement.style.overflow = 'hidden'
							loadFallbackTimer = null
						}, 4000)
					}

					const closeOverlay = () => {
						overlay.classList.remove('open')
						overlay.classList.remove('loading')
						document.documentElement.style.overflow = ''
						// cancel any pending fallback
						if (loadFallbackTimer) { window.clearTimeout(loadFallbackTimer); loadFallbackTimer = null }
						imgEl.onload = null
						// clear src after animation completes to avoid flashing
						setTimeout(() => { imgEl.src = '' }, 300)
					}

					closeBtn.addEventListener('click', closeOverlay)
					overlay.addEventListener('click', (e) => { if (e.target === overlay) closeOverlay() })
					document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && overlay.classList.contains('open')) closeOverlay() })

					const bindImages = (root: ParentNode = document) => {
						const imgs = Array.from(root.querySelectorAll<HTMLImageElement>('.VPDoc img:not(.vp-image-zoomable)'))
						imgs.forEach(img => {
							img.classList.add('vp-image-zoomable')
							// Precise tap detection: avoid opening on scroll or accidental touch-move
							let startX = 0, startY = 0, moved = false
							const threshold = 8 // px
							const onPointerDown = (ev: PointerEvent) => {
								startX = ev.clientX
								startY = ev.clientY
								moved = false
								const onPointerMove = (mv: PointerEvent) => {
									if (Math.hypot(mv.clientX - startX, mv.clientY - startY) > threshold) moved = true
								}
								const onPointerUp = (up: PointerEvent) => {
									window.removeEventListener('pointermove', onPointerMove)
									window.removeEventListener('pointerup', onPointerUp)
									if (!moved) {
										openOverlay(img.currentSrc || img.src, img.alt || '')
									}
								}
								window.addEventListener('pointermove', onPointerMove)
								window.addEventListener('pointerup', onPointerUp)
							}

							const onKey = (e: KeyboardEvent) => {
								if (e.key === 'Enter' || e.key === ' ') {
									e.preventDefault()
									openOverlay(img.currentSrc || img.src, img.alt || '')
								}
							}

							img.addEventListener('pointerdown', onPointerDown)
							img.addEventListener('keydown', onKey)
						})
					}

					// initial bind
					bindImages()
					// observe DOM for new content (client-side navigation)
					const mo = new MutationObserver((mutations) => {
						for (const m of mutations) {
							if (m.addedNodes && m.addedNodes.length) bindImages(m.target as ParentNode)
						}
					})
					mo.observe(document.body, { childList: true, subtree: true })
				}
				if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setupImageZoom)
				else setupImageZoom()
			}
	}
}

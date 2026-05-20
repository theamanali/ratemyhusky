import type { PlasmoCSConfig } from "plasmo"

export const config: PlasmoCSConfig = {
  matches: ["https://myplan.uw.edu/course/*"],
}

const API_BASE = "https://api.ratemydawg.com"

const cache: Record<string, Professor[]> = {}

const TOOLTIPS: Record<string, { title: string; desc: string }> = {
  QR: {
    title: "Quality Rating (RMP)",
    desc: "Average quality rating from RateMyProfessors student reviews, on a scale of 1–5. Higher is better.",
  },
  DR: {
    title: "Difficulty Rating (RMP)",
    desc: "Average difficulty rating from RateMyProfessors student reviews, on a scale of 1–5. Higher means more difficult.",
  },
  WTA: {
    title: "Would Take Again (RMP)",
    desc: "Percentage of RateMyProfessors reviewers who said they would take this professor again.",
  },
  CES: {
    title: "Course Evaluation Score",
    desc: "Weighted average of median responses across all UW course evaluation questions per section, weighted by students surveyed. Scale of 0–5. Higher is better.",
  },
}

function ratingColor(value: number, reverse = false, min = 1, max = 5): string {
  const t = Math.max(0, Math.min(1, reverse ? 1 - (value - min) / (max - min) : (value - min) / (max - min)))
  const r = Math.round(t < 0.5 ? 255 : 255 * (1 - t) * 2)
  const g = Math.round(t > 0.5 ? 255 : 255 * t * 2)
  return `rgb(${r}, ${g}, 0)`
}

function createTooltip() {
  const existing = document.getElementById("rmd-tooltip")
  if (existing) return existing

  const style = document.createElement("style")
  style.textContent = `
    #rmd-tooltip {
      position: fixed;
      background: #fff;
      color: rgb(33, 37, 41);
      border: 1px solid rgba(0,0,0,0.15);
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 13px;
      font-family: 'Open Sans', sans-serif;
      max-width: 360px;
      pointer-events: none;
      z-index: 99999;
      box-shadow: 0 4px 12px rgba(0,0,0,0.12);
      line-height: 1.5;
      display: none;
    }
    #rmd-tooltip strong {
      display: block;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 4px;
      color: rgb(33, 37, 41);
    }
    #rmd-tooltip span {
      color: rgb(90, 90, 90);
      font-size: 12px;
    }
  `
  document.head.appendChild(style)

  const tooltip = document.createElement("div")
  tooltip.id = "rmd-tooltip"
  document.body.appendChild(tooltip)

  document.addEventListener("mousemove", (e) => {
    tooltip.style.left = e.clientX + 14 + "px"
    tooltip.style.top = e.clientY + 14 + "px"
  })

  document.addEventListener("mouseover", (e) => {
    const pill = (e.target as Element).closest("[data-rmd-key]")
    if (!pill) return
    const key = pill.getAttribute("data-rmd-key")
    const info = TOOLTIPS[key]
    if (!info) return
    const statRaw = pill.getAttribute("data-rmd-stat")
    let statHtml = ""
    if (statRaw) {
      const { text, color } = JSON.parse(statRaw)
      const [num, ...rest] = text.split(" ")
      statHtml = `<span style="display:block; color:rgb(33,37,41); margin-bottom:4px; font-size:12px;"><span style="background:${color}; border-radius:4px; padding:1px 5px; font-weight:600; color:rgb(33,37,41); margin-right:3px;">${num}</span><span>${rest.join(" ")}</span></span>`
    }
    tooltip.innerHTML = `<strong>${info.title}</strong>${statHtml}<span>${info.desc}</span>`
    tooltip.style.display = "block"
  })

  document.addEventListener("mouseout", (e) => {
    const leaving = (e.target as Element).closest("[data-rmd-key]")
    const entering = (e.relatedTarget as Element)?.closest("[data-rmd-key]")
    if (leaving && leaving !== entering) tooltip.style.display = "none"
  })

  return tooltip
}

function pill(
  key: string,
  value: string | null,
  color: string | null,
  animate = true,
  statLine: string | null = null
): HTMLElement {
  const bg = color ?? "rgb(180,180,180)"
  const text = value ?? "N/A"

  const wrapper = document.createElement("span")
  wrapper.setAttribute("data-rmd-key", key)
  if (statLine) wrapper.setAttribute("data-rmd-stat", JSON.stringify({ text: statLine, color: bg }))
  wrapper.style.cssText =
    "display:inline-flex; align-items:center; gap:2px; font-size:11.375px; font-family:'Open Sans',sans-serif; white-space:nowrap; cursor:default;"

  const label = document.createElement("span")
  label.style.cssText = "color:rgb(33,37,41); font-weight:400;"
  label.textContent = key

  const box = document.createElement("span")
  const isNA = bg === "rgb(180,180,180)"
  const shouldAnimate = animate && !isNA
  box.style.cssText = `background:${shouldAnimate ? "rgb(255,0,0)" : bg}; border-radius:4px; padding:2px 5px; color:rgb(33,37,41); font-weight:600; display:inline-block; line-height:11.375px;${shouldAnimate ? " transition:background 0.6s ease;" : ""}`
  box.textContent = shouldAnimate ? (text.endsWith("%") ? "0%" : "0.0") : text

  if (shouldAnimate) {
    const isPercent = text.endsWith("%")
    const finalNum = parseFloat(text)
    const duration = 600
    const start = performance.now()

    requestAnimationFrame(function tick(now) {
      const progress = Math.min((now - start) / duration, 1)
      const current = finalNum * progress
      box.textContent = isPercent ? `${Math.round(current)}%` : current.toFixed(1)
      if (progress < 1) requestAnimationFrame(tick)
      else box.textContent = text
    })

    requestAnimationFrame(() => {
      requestAnimationFrame(() => { box.style.background = bg })
    })
  }

  wrapper.appendChild(label)
  wrapper.appendChild(box)
  return wrapper
}

interface Professor {
  avg_quality_rating: number | null
  avg_difficulty_rating: number | null
  would_take_again_percent: number | null
  avg_eval_median_weighted: number | null
  rmp_rating_count: number | null
  cec_surveyed_count: number | null
  cec_eval_count: number | null
}

function injectBadge(el: HTMLElement, prof: Professor, animate = true) {
  if (el.querySelector(".rmd-badge")) return

  const badge = document.createElement("div")
  badge.className = "rmd-badge"
  badge.style.cssText =
    "display:flex; gap:4px; align-items:center; flex-wrap:nowrap; margin-top:0; opacity:0; transition:opacity 0.1s ease;"
  requestAnimationFrame(() => {
    requestAnimationFrame(() => { badge.style.opacity = "1" })
  })

  const { avg_quality_rating: qr, avg_difficulty_rating: dr, would_take_again_percent: wta, avg_eval_median_weighted: ces, rmp_rating_count: rmpCount, cec_surveyed_count: cecCount, cec_eval_count: cecEvals } = prof

  const rmpSuffix = rmpCount ? ` from <b>${rmpCount}</b> reviews` : ""
  const cecSuffix = cecCount && cecEvals ? ` calculated from <b>${cecCount}</b> surveys across <b>${cecEvals}</b> sections` : ""

  badge.appendChild(pill("QR", qr != null ? qr.toFixed(1) : null, qr != null ? ratingColor(qr) : null, animate, qr != null ? `${qr.toFixed(1)} calculated${rmpSuffix}` : null))
  badge.appendChild(pill("DR", dr != null ? dr.toFixed(1) : null, dr != null ? ratingColor(dr, true) : null, animate, dr != null ? `${dr.toFixed(1)} calculated${rmpSuffix}` : null))
  badge.appendChild(pill("WTA", wta != null ? `${wta.toFixed(0)}%` : null, wta != null ? ratingColor(wta, false, 0, 100) : null, animate, wta != null ? `${wta.toFixed(0)}% of <b>${rmpCount}</b> reviewers would take again` : null))
  badge.appendChild(pill("CES", ces != null ? ces.toFixed(1) : null, ces != null ? ratingColor(ces, false, 0, 5) : null, animate, ces != null ? `${ces.toFixed(1)}${cecSuffix}` : null))

  el.appendChild(badge)
}

async function matchAndInject() {
  // Handle both single instructor (div.mb-1) and multiple instructors (ul.mb-1 > li)
  const instructorEls: HTMLElement[] = []
  document.querySelectorAll<HTMLElement>(".cdpSectionsTable .mb-1").forEach((el) => {
    if (el.tagName === "UL") {
      el.querySelectorAll<HTMLElement>("li").forEach((li) => {
        if (li.textContent?.trim()) instructorEls.push(li)
      })
    } else if (el.textContent?.trim()) {
      instructorEls.push(el)
    }
  })

  if (!instructorEls.length) return

  const names = [...new Set(instructorEls.map((el) => el.textContent!.trim()))]
  const uncached = names.filter((n) => !(n in cache))

  if (uncached.length) {
    try {
      const res = await fetch(`${API_BASE}/professors/match/batch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names: uncached }),
      })
      if (res.ok) {
        const data = await res.json()
        Object.assign(cache, data)
      }
    } catch {}
  }

  createTooltip()

  let animateCount = 0
  const animatedNames = new Set<string>()

  instructorEls.forEach((el) => {
    const name = el.textContent!.trim()
    const matches = cache[name]
    if (!matches?.length) return
    const alreadyInjected = !!el.querySelector(".rmd-badge")
    if (alreadyInjected) { injectBadge(el, matches[0], false); return }
    const shouldAnimate = !animatedNames.has(name) && animateCount < 2
    if (shouldAnimate) { animatedNames.add(name); animateCount++ }
    injectBadge(el, matches[0], shouldAnimate)
  })
}

function waitForTable(callback: () => void) {
  if (document.querySelector(".cdpSectionsTable")) {
    callback()
    return
  }
  const observer = new MutationObserver(() => {
    if (document.querySelector(".cdpSectionsTable")) {
      observer.disconnect()
      callback()
    }
  })
  observer.observe(document.body, { childList: true, subtree: true })
}

// Run when table appears, re-run on URL changes (MyPlan is a SPA)
waitForTable(matchAndInject)

let lastUrl = location.href
let debounceTimer: ReturnType<typeof setTimeout> | null = null
new MutationObserver(() => {
  if (location.href !== lastUrl) {
    lastUrl = location.href
    if (debounceTimer) clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => waitForTable(matchAndInject), 500)
  }
}).observe(document.body, { childList: true, subtree: true })

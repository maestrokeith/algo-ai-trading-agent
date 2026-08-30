import { useEffect, useState } from 'react'

const INITIAL_POINTS = [38, 36, 35, 33, 34, 31, 28, 26, 25, 22, 20, 18, 16, 14, 12, 10, 8, 9, 7, 6, 4]
const WIDTH = 320
const HEIGHT = 48

function buildPath(points: number[]) {
  if (points.length < 2) return { line: '', fill: '' }
  const step = WIDTH / (points.length - 1)
  const coords = points.map((y, i) => `${i * step},${y}`)
  const line = `M${coords.join(' L')}`
  const lastX = (points.length - 1) * step
  const fill = `${line} L${lastX},${HEIGHT} L0,${HEIGHT} Z`
  return { line, fill, lastX, lastY: points[points.length - 1] }
}

export function Sparkline() {
  const [points, setPoints] = useState(INITIAL_POINTS)
  const [flash, setFlash] = useState(false)

  useEffect(() => {
    const id = setInterval(() => {
      setPoints((prev) => {
        const last = prev[prev.length - 1]
        const next = Math.max(2, Math.min(HEIGHT - 2, last + (Math.random() - 0.48) * 3))
        const updated = [...prev.slice(-23), next]
        return updated
      })
      setFlash(true)
      setTimeout(() => setFlash(false), 400)
    }, 3000)
    return () => clearInterval(id)
  }, [])

  const { line, fill, lastX = WIDTH, lastY = 4 } = buildPath(points)

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      style={{ width: '100%', height: HEIGHT, overflow: 'visible' }}
      aria-label="Equity sparkline"
    >
      <defs>
        <linearGradient id="spark-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#FFB300" stopOpacity={0.25} />
          <stop offset="100%" stopColor="#FFB300" stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={fill} fill="url(#spark-grad)" stroke="none" />
      <path
        d={line}
        fill="none"
        stroke={flash ? '#FFC940' : '#FFB300'}
        strokeWidth={1.5}
        strokeLinejoin="round"
        style={{ transition: 'stroke 0.4s ease' }}
      />
      <circle cx={lastX} cy={lastY} r={3} fill="#FFB300" opacity={0.9}>
        <animate attributeName="r" values="3;5;3" dur="2s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.9;0.4;0.9" dur="2s" repeatCount="indefinite" />
      </circle>
    </svg>
  )
}

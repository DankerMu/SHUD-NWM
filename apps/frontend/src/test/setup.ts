import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
})

class TestResizeObserver implements ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(globalThis, 'ResizeObserver', {
  writable: true,
  configurable: true,
  value: TestResizeObserver,
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  configurable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})

if (!window.PointerEvent) {
  window.PointerEvent = MouseEvent as typeof PointerEvent
}

Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
  writable: true,
  configurable: true,
  value: vi.fn(),
})

Object.defineProperty(HTMLElement.prototype, 'hasPointerCapture', {
  writable: true,
  configurable: true,
  value: vi.fn(() => false),
})

Object.defineProperty(HTMLElement.prototype, 'setPointerCapture', {
  writable: true,
  configurable: true,
  value: vi.fn(),
})

Object.defineProperty(HTMLElement.prototype, 'releasePointerCapture', {
  writable: true,
  configurable: true,
  value: vi.fn(),
})

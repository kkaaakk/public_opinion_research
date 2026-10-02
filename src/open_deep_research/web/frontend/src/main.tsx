import React from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app/App.tsx'
import './trajectory/upstream-theme/base.css'
import './trajectory/upstream-theme/design-platform.css'
import './trajectory/upstream-theme/focus.css'
import './trajectory/upstream-theme/scrollbar.css'
import './trajectory/upstream-theme/shiki.css'
import './app/shell.css'

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)

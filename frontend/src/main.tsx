import React, { Component, type ErrorInfo, type ReactNode } from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles.css'
import './brand.css'

class WorkbenchErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() { return { failed: true } }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('workbench render error', error.message, info.componentStack)
  }

  render() {
    if (!this.state.failed) return this.props.children
    return <main className="app-error-boundary" role="alert">
      <section>
        <h1>页面刚才没有正常显示</h1>
        <p>原有文案、归档和任务仍保存在本机。你可以先重新加载；如果仍然失败，再查看工作台日志。</p>
        <div>
          <button onClick={() => this.setState({ failed: false })}>重试页面</button>
          <button onClick={() => window.location.reload()}>重新加载工作台</button>
        </div>
      </section>
    </main>
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><WorkbenchErrorBoundary><App /></WorkbenchErrorBoundary></React.StrictMode>,
)

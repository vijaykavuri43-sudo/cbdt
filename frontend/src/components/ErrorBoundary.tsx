import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  fallback?: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: any) {
    console.error('[ErrorBoundary]', error, info?.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        this.props.fallback ?? (
          <div
            style={{
              padding: 24,
              background: 'var(--panel)',
              border: '1px solid var(--bad)',
              borderRadius: 10,
              margin: 16,
            }}
          >
            <h3 style={{ color: 'var(--bad)', margin: '0 0 8px' }}>Something went wrong</h3>
            <div className="small dim" style={{ marginBottom: 12 }}>
              {this.state.error?.message ?? 'Unknown error'}
            </div>
            <button
              className="btn"
              onClick={() => {
                this.setState({ hasError: false, error: null })
              }}
            >
              Try again
            </button>
          </div>
        )
      )
    }
    return this.props.children
  }
}

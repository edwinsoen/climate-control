import type { ReactNode } from 'react'

export function CardHeader({ name, testId, description, actions }: {
  name: string; testId: string; description: ReactNode; actions: ReactNode
}) {
  return <header className="card-header">
    <div className="card-heading">
      <h3 data-testid={testId}>{name}</h3>
      <div className="card-actions">{actions}</div>
    </div>
    <p className="card-description">{description}</p>
  </header>
}

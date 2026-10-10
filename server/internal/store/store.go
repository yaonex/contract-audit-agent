package store

import (
	"context"

	"github.com/yaonex/contract-audit-agent/server/internal/task"
)

// Store persists audit tasks. All implementations (memory / sqlite / postgres)
// must behave identically, especially ClaimNext under concurrent workers.
type Store interface {
	Create(ctx context.Context, t *task.Task) error
	Get(ctx context.Context, id string) (*task.Task, error)
	UpdateStatus(ctx context.Context, id string, from, to task.Status) error
	ClaimNext(ctx context.Context) (*task.Task, error) // PG: SELECT ... FOR UPDATE SKIP LOCKED
}
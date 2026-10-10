// Package task: task domain model and state machine.
package task

import (
	"fmt"
	"time"
)

type Status string

const (
	StatusPending   Status = "pending"
	StatusRunning   Status = "running"
	StatusDone      Status = "done"
	StatusFailed    Status = "failed"
	StatusCancelled Status = "cancelled"
)

// allowed defines legal state transitions.
var allowed = map[Status][]Status{
	StatusPending: {StatusRunning, StatusCancelled},
	StatusRunning: {StatusDone, StatusFailed, StatusCancelled},
}

type Task struct {
	ID         string
	Filename   string
	Source     string
	Status     Status
	ReportJSON string
	Error      string
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

// Transition moves the task to a new status or returns an error if illegal.
func (t *Task) Transition(to Status) error {
	for _, s := range allowed[t.Status] {
		if s == to {
			t.Status = to
			t.UpdatedAt = time.Now()
			return nil
		}
	}
	return fmt.Errorf("illegal transition %s -> %s", t.Status, to)
}

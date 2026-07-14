package account

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"path/filepath"
	"testing"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/persistence/relational"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
)

type buildDetectionAdapter struct {
	status int
	model  string
}

func (a *buildDetectionAdapter) Provider() accountdomain.Provider { return accountdomain.ProviderBuild }

func (a *buildDetectionAdapter) ListModels(context.Context, accountdomain.Credential) ([]string, error) {
	return []string{"grok-4.5"}, nil
}

func (a *buildDetectionAdapter) ForwardResponse(_ context.Context, request provider.ResponseResourceRequest) (*provider.Response, error) {
	a.model = request.Model
	return &provider.Response{StatusCode: a.status, Body: io.NopCloser(bytes.NewReader(nil))}, nil
}

func TestDetectBuildAccountsUsesAvailableModelAndPreservesAccountOnProbeFailure(t *testing.T) {
	ctx := context.Background()
	database, err := relational.OpenSQLite(ctx, filepath.Join(t.TempDir(), "detection.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = database.Close() })
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	repository := relational.NewAccountRepository(database)
	credential, _, err := repository.UpsertByIdentity(ctx, accountdomain.Credential{
		Provider: accountdomain.ProviderBuild, AuthType: accountdomain.AuthTypeOAuth, Name: "build", SourceKey: "build-detection",
		EncryptedAccessToken: "token", Enabled: true, AuthStatus: accountdomain.AuthStatusActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	adapter := &buildDetectionAdapter{status: http.StatusNotFound}
	service := NewService(repository, nil, nil, nil, provider.NewRegistry(adapter), nil, nil)
	result, err := service.DetectBuildAccounts(ctx, []uint64{credential.ID})
	if err != nil {
		t.Fatal(err)
	}
	if result.Failed != 1 || result.Succeeded != 0 || adapter.model != "grok-4.5" {
		t.Fatalf("result = %#v, probe model = %q", result, adapter.model)
	}
	stored, err := repository.Get(ctx, credential.ID)
	if err != nil {
		t.Fatal(err)
	}
	if !stored.Enabled || stored.AuthStatus != accountdomain.AuthStatusActive || stored.LastError != "检测失败: HTTP 404" {
		t.Fatalf("stored credential = %#v", stored)
	}
}

func TestChooseBuildDetectionModelPrefersObservedAvailableModel(t *testing.T) {
	if got := chooseBuildDetectionModel("grok-account-model", []string{"grok-4.5", "grok-account-model"}); got != "grok-account-model" {
		t.Fatalf("model = %q", got)
	}
}

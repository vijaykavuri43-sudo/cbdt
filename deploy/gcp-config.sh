#!/usr/bin/env bash
# Shared operator identity and destination for all deployment entry points.
# The VM service account continues to handle application and backup GCS access.
GCP_ACCOUNT="${GCP_ACCOUNT:-jk5391775@gmail.com}"
GCP_PROJECT="${GCP_PROJECT:-project-9641a7b9-3759-4c98-9b8}"
GCP_ZONE="${GCP_ZONE:-asia-south1-a}"
GCP_BILLING_ACCOUNT="${GCP_BILLING_ACCOUNT:-01D42C-5BB8A5-0CD6DA}"
GCP_ORGANIZATION="${GCP_ORGANIZATION:-580417351522}"
export GCP_ACCOUNT GCP_PROJECT GCP_ZONE GCP_BILLING_ACCOUNT GCP_ORGANIZATION

# Use the dedicated operator login when configured, without changing other apps.
if [[ -n "${GCP_CLOUDSDK_CONFIG:-}" ]]; then
  export CLOUDSDK_CONFIG="$GCP_CLOUDSDK_CONFIG"
elif [[ -z "${CLOUDSDK_CONFIG:-}" && -d "$HOME/.config/gcloud-s3lm" ]]; then
  export CLOUDSDK_CONFIG="$HOME/.config/gcloud-s3lm"
fi
# Prefer a native Linux SDK when WSL's PATH selects the Windows installation.
if [[ -z "${GCLOUD_BIN:-}" ]]; then
  if [[ -x "$HOME/google-cloud-sdk/bin/gcloud" ]]; then
    GCLOUD_BIN="$HOME/google-cloud-sdk/bin/gcloud"
  else
    GCLOUD_BIN=gcloud
  fi
fi
gcloud() {
  command "$GCLOUD_BIN" --account="$GCP_ACCOUNT" "$@"
}

gcp_check_project() {
  local billing_info billing_link billing_enabled organization
  billing_info=$(gcloud billing projects describe "$GCP_PROJECT" \
    --format='value(billingAccountName,billingEnabled)') || return
  read -r billing_link billing_enabled <<< "$billing_info"
  if [[ "$billing_link" != "billingAccounts/$GCP_BILLING_ACCOUNT" || \
        "${billing_enabled,,}" != "true" ]]; then
    echo "Deployment stopped: $GCP_PROJECT must have active billing on $GCP_BILLING_ACCOUNT." >&2
    return 1
  fi
  organization=$(gcloud projects describe "$GCP_PROJECT" --format='value(parent.id)') || return
  if [[ "$organization" != "$GCP_ORGANIZATION" ]]; then
    echo "Deployment stopped: $GCP_PROJECT must belong to organization $GCP_ORGANIZATION." >&2
    return 1
  fi
  echo "Verified: $GCP_ACCOUNT → $GCP_PROJECT (organization $organization, billing $GCP_BILLING_ACCOUNT)"
}

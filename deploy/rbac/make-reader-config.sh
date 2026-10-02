#!/usr/bin/env bash
# infra_agent 전용 읽기 계정(infra-agent-reader)의 kubeconfig를 표준 출력으로 만듭니다.
#
# 사용 (클러스터 관리자 kubectl이 있는 곳에서 실행, 결과 파일은 커밋하지 마세요):
#   umask 077
#   deploy/rbac/make-reader-config.sh https://127.0.0.1:16443 168h > infra-agent-reader.kubeconfig
#
# 인자:
#   1) infra_agent가 접속할 API 서버 주소 (SSH 터널을 쓰면 터널의 로컬 주소)
#   2) 토큰 유효 기간 (기본 168h). 만료되면 다시 만듭니다.
#
# 관리자 kubeconfig는 CA 인증서를 읽고 토큰을 발급하는 데만 쓰며, 결과 파일에는 들어가지 않습니다.
# 결과 파일에는 ServiceAccount 토큰만 들어갑니다(클라이언트 인증서 없음).
set -euo pipefail

SERVER="${1:?API 서버 주소가 필요합니다 (예: https://127.0.0.1:16443)}"
DURATION="${2:-168h}"
NAMESPACE="infra-agent"
ACCOUNT="infra-agent-reader"

case "$SERVER" in
  https://*) ;;
  *) echo "API 서버 주소는 https:// 로 시작해야 합니다" >&2; exit 2 ;;
esac

CA_DATA="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
if [ -z "$CA_DATA" ]; then
  echo "현재 kubectl 컨텍스트에서 certificate-authority-data를 찾지 못했습니다" >&2
  exit 1
fi
TOKEN="$(kubectl -n "$NAMESPACE" create token "$ACCOUNT" --duration="$DURATION")"

cat <<CONFIG
apiVersion: v1
kind: Config
current-context: infra-agent-reader
clusters:
  - name: infra-agent-cluster
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA_DATA}
users:
  - name: ${ACCOUNT}
    user:
      token: ${TOKEN}
contexts:
  - name: infra-agent-reader
    context:
      cluster: infra-agent-cluster
      user: ${ACCOUNT}
CONFIG

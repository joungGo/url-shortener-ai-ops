# WF11: WF10 relay가 WF07 이미지 부재 확인(ecr:DescribeImages)에 쓰는 IRSA 역할.
# 기본 비활성. 클러스터의 EKS OIDC provider(운영 root가 생성)를 조회만 하며 새로 만들지 않는다.
# 신뢰 대상은 WF10 namespace의 정확한 ServiceAccount 하나이고, 권한은 urlshortener 저장소 조회 하나다.
variable "enable_wf11_recovery_irsa" {
  description = "WF10 relay의 ECR 이미지 조회 IRSA 역할 활성화"
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_wf11_recovery_irsa || var.enable_ai_ops_registry
    error_message = "WF11 복구 IRSA는 AI 등록 기반과 함께 활성화해야 합니다."
  }
}

variable "wf10_namespace" {
  description = "WF10 배포 namespace(사용자 결정 2026-11-25: ai-ops)"
  type        = string
  default     = "ai-ops"
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.wf10_namespace))
    error_message = "namespace 이름 형식이 아닙니다."
  }
}

variable "wf10_service_account" {
  description = "WF10 Pod의 기존 ServiceAccount 이름(renderer 입력 service_account와 같아야 함)"
  type        = string
  default     = "ai-ops"
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.wf10_service_account))
    error_message = "ServiceAccount 이름 형식이 아닙니다."
  }
}

data "aws_eks_cluster" "wf11" {
  count = var.enable_wf11_recovery_irsa ? 1 : 0
  name  = "urlshortener"
}

data "aws_iam_openid_connect_provider" "wf11_eks" {
  count = var.enable_wf11_recovery_irsa ? 1 : 0
  url   = data.aws_eks_cluster.wf11[0].identity[0].oidc[0].issuer
}

locals {
  wf11_issuer = var.enable_wf11_recovery_irsa ? replace(data.aws_eks_cluster.wf11[0].identity[0].oidc[0].issuer, "https://", "") : ""
}

resource "aws_iam_role" "wf11_recovery" {
  count = var.enable_wf11_recovery_irsa ? 1 : 0
  name  = "${var.project_name}-recovery-ecr-read"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
      Principal = { Federated = data.aws_iam_openid_connect_provider.wf11_eks[0].arn },
      Condition = { StringEquals = {
        "${local.wf11_issuer}:aud" = "sts.amazonaws.com",
        "${local.wf11_issuer}:sub" = "system:serviceaccount:${var.wf10_namespace}:${var.wf10_service_account}"
      } }
    }]
  })
  max_session_duration = 3600
  tags                 = { Environment = var.environment, Component = "wf11-recovery" }
}

resource "aws_iam_role_policy" "wf11_recovery" {
  count = var.enable_wf11_recovery_irsa ? 1 : 0
  name  = "wf11-recovery-ecr-describe"
  role  = aws_iam_role.wf11_recovery[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:DescribeImages"], Resource = "arn:aws:ecr:${var.aws_region}:${var.expected_account_id}:repository/urlshortener" }
  ] })
}

output "wf11_recovery_role_name" {
  description = "WF10 recovery 입력 aws_role_name과 ServiceAccount의 eks.amazonaws.com/role-arn에 쓰는 역할"
  value       = try(aws_iam_role.wf11_recovery[0].name, null)
}

output "wf11_recovery_role_arn" {
  description = "WF10 ServiceAccount annotation eks.amazonaws.com/role-arn 값"
  value       = try(aws_iam_role.wf11_recovery[0].arn, null)
}

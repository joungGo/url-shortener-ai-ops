"""WF11 relay IRSA role (terraform/ai-ops-registry/wf11-recovery.tf): static boundary contract.

terraform fmt/init/validate run in CI; this pins what the role may do and trust."""
from pathlib import Path
import re
import unittest

TF = Path(__file__).resolve().parents[2] / 'terraform' / 'ai-ops-registry' / 'wf11-recovery.tf'


class RecoveryIrsaTerraformTest(unittest.TestCase):
    def setUp(self):
        self.text = TF.read_text()

    def block(self, kind, name):
        match = re.search(rf'{kind} "{re.escape(name)}" \{{\n(.*?)\n\}}\n', self.text, re.S)
        self.assertIsNotNone(match, (kind, name))
        return match.group(1)

    def test_disabled_by_default_and_tied_to_the_registry_switch(self):
        flag = self.block('variable', 'enable_wf11_recovery_irsa')
        self.assertIn('default     = false', flag)
        self.assertIn('!var.enable_wf11_recovery_irsa || var.enable_ai_ops_registry', flag)
        for kind, label in (('data', 'aws_eks_cluster" "wf11'), ('data', 'aws_iam_openid_connect_provider" "wf11_eks'),
                            ('resource', 'aws_iam_role" "wf11_recovery'),
                            ('resource', 'aws_iam_role_policy" "wf11_recovery')):
            with self.subTest(block=label):
                self.assertIn('count = var.enable_wf11_recovery_irsa ? 1 : 0', self.block(kind, label))

    def test_trusts_only_the_exact_wf10_service_account_via_the_cluster_oidc(self):
        role = self.block('resource', 'aws_iam_role" "wf11_recovery')
        self.assertIn('Action = "sts:AssumeRoleWithWebIdentity"', role)
        self.assertIn('Federated = data.aws_iam_openid_connect_provider.wf11_eks[0].arn', role)
        self.assertIn('"${local.wf11_issuer}:sub" = "system:serviceaccount:${var.wf10_namespace}:${var.wf10_service_account}"', role)
        self.assertIn('"${local.wf11_issuer}:aud" = "sts.amazonaws.com"', role)
        self.assertNotIn('StringLike', role)
        self.assertNotIn('*', role)
        # The cluster OIDC provider is looked up, never created here.
        self.assertNotIn('resource "aws_iam_openid_connect_provider"', self.text)

    def test_policy_allows_only_describe_images_on_one_repository(self):
        policy = self.block('resource', 'aws_iam_role_policy" "wf11_recovery')
        self.assertEqual(re.findall(r'Action = \[(.*?)\]', policy), ['"ecr:DescribeImages"'])
        self.assertIn('Resource = "arn:aws:ecr:${var.aws_region}:${var.expected_account_id}:repository/urlshortener"', policy)
        self.assertNotIn('*', policy)
        self.assertNotIn('aws_iam_role_policy_attachment', self.text)

    def test_defaults_match_the_wf11_placement_decision(self):
        self.assertIn('default     = "ai-ops"', self.block('variable', 'wf10_namespace'))
        self.assertIn('default     = "ai-ops"', self.block('variable', 'wf10_service_account'))
        self.assertIn('name  = "${var.project_name}-recovery-ecr-read"', self.block('resource', 'aws_iam_role" "wf11_recovery'))


if __name__ == '__main__':
    unittest.main()

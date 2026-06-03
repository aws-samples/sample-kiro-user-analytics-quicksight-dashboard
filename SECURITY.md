# Security

This document captures the security posture of the Sample Kiro User Analytics Dashboard:
the threat model in summary, the shared responsibility split between AWS, this
solution's defaults, and the customer; and per-service security guidance for
each AWS service the solution uses.

To report a security vulnerability, see
[Reporting security issues](#reporting-security-issues) below — please do **not**
open a public GitHub issue.

## Reporting security issues

Amazon Web Services (AWS) is dedicated to the responsible disclosure of security
vulnerabilities.

We kindly ask that you **do not** open a public GitHub issue to report security
concerns.

Instead, please submit the issue to the AWS Vulnerability Disclosure Program via
[HackerOne](https://hackerone.com/aws_vdp) or send your report via
[email](mailto:aws-security@amazon.com).

For more details, visit the
[AWS Vulnerability Reporting Page](http://aws.amazon.com/security/vulnerability-reporting/).

Thank you in advance for collaborating with us to help protect our customers.

## Shared responsibility

This solution operates inside the
[AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/).
Each service used here splits security obligations as follows:

| Layer | Responsibility | Owner |
|-------|---------------|-------|
| Underlying infrastructure (hardware, host OS, hypervisor) | Security *of* the cloud | AWS |
| Service implementation (Amazon S3, AWS Glue, Amazon Athena, Amazon QuickSight, AWS IAM, AWS KMS managed control plane) | Security *of* the cloud | AWS |
| CloudFormation templates + scripts shipped in this repo (default configurations) | Security *in* the cloud, partially | This solution |
| AWS account configuration (root account, MFA, organization SCPs, Security Hub) | Security *in* the cloud | Customer |
| Identity and access decisions (who can run the deploy script, who is granted Amazon QuickSight access, who reads the dashboard) | Security *in* the cloud | Customer |
| Data classification + handling (whether to hash emails, whether to enable Row-Level Security, retention beyond what we configure) | Security *in* the cloud | Customer |
| Whether to resolve user GUIDs to real names via IAM Identity Center, and the cross-region/cross-account transfer of that personal data this implies | Security *in* the cloud | Customer |
| IAM Identity Center directory contents, the cross-account trust policy for `IDC_ROLE_ARN`, and scheduling deletion of the retained identity-map KMS key | Security *in* the cloud | Customer |
| Network controls around access to the dashboard (SSO, VPN, IP-allow-listing on Amazon QuickSight) | Security *in* the cloud | Customer |
| Source bucket security (Kiro logs bucket, since it is created and owned by the customer outside this stack) | Security *in* the cloud | Customer |
| Encryption keys for the source bucket and AWS Glue Data Catalog (account-wide settings outside this stack) | Security *in* the cloud | Customer |

Items marked **This solution** are the defaults the CloudFormation templates
and scripts in this repo apply on the customer's behalf. Customers can change
those defaults; the table in [Solution defaults](#solution-defaults) lists what
they are.

## Solution defaults

What the CloudFormation templates and the deployment scripts configure by default:

| Default | Resource | Configuration |
|---------|----------|---------------|
| Block all public access | Athena results bucket | All four `PublicAccessBlockConfiguration` settings = `true` |
| TLS-only access | Athena results bucket | `aws:SecureTransport=false` denied via bucket policy |
| Encryption at rest | Athena results bucket | `SSEAlgorithm: AES256` (SSE-S3) |
| Object ownership | Athena results bucket | `BucketOwnerEnforced` (ACLs disabled) |
| Versioning | Athena results bucket | Enabled, with non-current expiration lifecycle |
| Server access logging | Athena results bucket | Opt-in for the customer (CloudTrail covers Athena API events at the account level) |
| Lifecycle expiration | Athena results bucket | 30 days for query results; 7 days for non-current versions |
| Athena workgroup enforcement | Athena workgroup | `EnforceWorkGroupConfiguration: true` (per-query overrides denied) |
| Athena result encryption | Athena workgroup | `EncryptionOption: SSE_S3` |
| Athena CloudWatch metrics | Athena workgroup | `PublishCloudWatchMetricsEnabled: true` |
| AWS Glue crawler IAM | Crawler role | Three resource-scoped inline policies; no AWS-managed policy attached |
| Amazon QuickSight S3 IAM | Inline policy on the QS service role | Resource ARNs scoped to the two buckets the dashboard reads/writes; the QS-console-managed `AWSQuickSightS3Policy` is left untouched |
| Amazon QuickSight SPICE encryption | Datasets | Service-managed encryption (Enterprise edition default) |

The following defaults apply **only when `IDENTITY_MAPPING=true`** (the optional `cfn/03-identity-mapping.yaml` stack):

| Default | Resource | Configuration |
|---------|----------|---------------|
| Dedicated PII bucket | Identity-map bucket | Separate from all other solution data; BPA on all four settings; SSE-KMS with a dedicated customer-managed key |
| Encryption key | Identity-map KMS key | Customer-managed CMK, `EnableKeyRotation: true`, key policy delegates to IAM via the account-root statement (no broad grants) |
| Server access logging | Identity-map bucket | Enabled (the bucket holds PII), to a dedicated log bucket with a 90-day lifecycle |
| TLS-only + versioning + lifecycle | Identity-map bucket | `DenyInsecureTransport` policy; versioning on; 30-day noncurrent-version expiry |
| Least-privilege Lambda role | Identity-map Lambda role | `identitystore:ListUsers` scoped to the specific store ARN; Athena/Glue scoped to this stack's workgroup/database; S3 scoped to the named buckets; `kms:Encrypt`/`GenerateDataKey` on the one CMK; cross-account `sts:AssumeRole` and source `kms:Decrypt` granted only when the matching parameter is set |
| Reserved concurrency | Identity-map Lambda | `ReservedConcurrentExecutions: 1` (the refresh is a singleton) |
| Fail-closed refresh | Identity-map Lambda | On any error it leaves the previous CSV intact rather than overwriting with empty/partial output |

## Per-service security guidance

### Amazon S3

Two solution-managed buckets and one customer-owned bucket are involved.

**Athena results bucket** (created by this stack):
- Public access blocked (all four settings).
- TLS-only via a `DenyInsecureTransport` bucket policy.
- SSE-S3 (`AES256`) encryption at rest.
- `BucketOwnerEnforced` ownership (S3 ACLs disabled).
- Versioning enabled.
- Server access logging is intentionally not configured (opt-in for the customer; CloudTrail covers Athena API events at the account level). The `cfn_nag` W35 / Checkov CKV_AWS_18 finding is suppressed inline with this rationale.
- Lifecycle: 30-day expiration on query results; 7-day expiration on non-current versions.

**Identity-map bucket** (created by this stack only when `IDENTITY_MAPPING=true`):
- Holds resolved names/emails (PII), so it is **isolated from the Athena results bucket** and hardened further: SSE-KMS with a dedicated customer-managed key, server access logging enabled (to a dedicated log bucket), versioning, a TLS-only `DenyInsecureTransport` policy, BPA on all four settings, and a noncurrent-version lifecycle.
- Written only by the identity-map Lambda; read only by the QuickSight service role (granted `s3:GetObject` + `kms:Decrypt` via the inline policy the deploy script manages).
- Emptied (all versions) and deleted on opt-out and on teardown; see the [Identity mapping](#identity-mapping-optional) section.

**Kiro logs bucket** (customer-owned, pre-existing):
- The customer is responsible for bucket-level controls: Public Access Block, encryption, versioning, lifecycle, and the Kiro export service principal's `s3:PutObject` grant.
- This solution requests only `s3:GetObject`, `s3:GetBucketLocation`, and `s3:ListBucket` on this bucket via the AWS Glue crawler role.
- If the bucket is encrypted with a customer-managed AWS KMS key, set `KMS_KEY_ARN` so the crawler role is granted `kms:Decrypt` and `kms:DescribeKey` on that specific key. The KMS key policy must also list the crawler role as a principal allowed to use the key.

### AWS IAM

- The AWS Glue crawler role uses three resource-scoped inline policies. No AWS-managed policy is attached. The trust policy restricts `sts:AssumeRole` to the `glue.amazonaws.com` service principal.
  - `GlueCatalogRW`: catalog/database/tables/partitions actions limited to this stack's database ARN.
  - `ReadKiroLogsBucket`: `s3:GetObject` + `s3:GetBucketLocation` + `s3:ListBucket` scoped to the Kiro logs bucket; conditional `kms:Decrypt` + `kms:DescribeKey` on the customer-supplied KMS key.
  - `CrawlerObservability`: `logs:CreateLogGroup` + `logs:CreateLogStream` + `logs:PutLogEvents` on `/aws-glue/*`; `cloudwatch:PutMetricData` constrained to the `AWS/Glue` namespace via a Condition.
- The Amazon QuickSight service role gets an additive inline policy named `KiroAnalyticsQuickSightS3Access`. It does not modify the console-managed `AWSQuickSightS3Policy`, so the Amazon QuickSight console retains ownership of that policy.
- The inline policy on the QuickSight role contains one `s3:ListAllMyBuckets` statement on `arn:aws:s3:::*`. This action does not support resource-scoped ARNs at the AWS API level; the wildcard reveals only bucket names, not contents, and mirrors the default behavior of the Amazon QuickSight console.

### AWS Glue

- Tables are created in a dedicated database namespaced by `STACK_PREFIX` so multiple parallel deployments do not collide.
- The crawler is configured with `TableLevelConfiguration: 6` and `SchemaChangePolicy: UPDATE_IN_DATABASE / DeleteBehavior: LOG`, so new per-model `<model>_messages` columns merge into the existing table instead of producing new tables.
- AWS Glue Data Catalog encryption-at-rest is an account+region-wide setting (`AWS::Glue::DataCatalogEncryptionSettings`), not a per-database property. This stack does not toggle it because doing so would affect every other AWS Glue database in the customer's account. To enable it once per account/region, run:
  ```bash
  aws glue put-data-catalog-encryption-settings --data-catalog-encryption-settings \
      'EncryptionAtRest={CatalogEncryptionMode=SSE-KMS-WITH-SERVICE-MANAGED-KEY}'
  ```

### Amazon Athena

- A dedicated workgroup is created (namespaced by `STACK_PREFIX`).
- `EnforceWorkGroupConfiguration: true` denies per-query overrides of the result location and encryption.
- `ResultConfiguration.EncryptionConfiguration.EncryptionOption: SSE_S3` enforces server-side encryption on every query result.
- `PublishCloudWatchMetricsEnabled: true` emits per-query metrics to CloudWatch (DataScannedInBytes, EngineExecutionTime, etc.).
- The audit trail for query starts/stops is via the AWS::Athena `StartQueryExecution` and `GetQueryExecution` API events recorded in AWS CloudTrail. CloudTrail is an account-level configuration that is the customer's responsibility.

### Amazon QuickSight

- SPICE encryption is service-managed (Enterprise edition default). There is no CloudFormation property that configures SPICE encryption.
- All datasets are owned by the principal passed via `QS_PRINCIPAL_ARN` at deploy time. Sharing the dashboard with additional users / groups is the customer's responsibility, and is configured through the Amazon QuickSight console or `aws quicksight update-dashboard-permissions`.
- The dashboard does not have Row-Level Security (RLS) configured by default. Any principal with `quicksight:QueryDashboard` permission sees every user's data. To restrict per-row visibility, add a Row-Level Security dataset rule keyed on `subscription_tier`, `user_id`, or another column. See the README "Customization options" section.
- `email` is ingested into SPICE in plaintext unless `HASH_EMAILS=true` is set on the deploy. When set, the Athena view layer wraps the column in `to_hex(sha256(...))` so plaintext does not reach SPICE.
- Network access to the dashboard (SSO, VPN, IP-allow-listing) is configured at the Amazon QuickSight account level and is the customer's responsibility.

### AWS KMS (optional)

- If the Kiro logs bucket is encrypted with a customer-managed AWS KMS key, pass `KMS_KEY_ARN` at deploy time. The AWS Glue crawler role is then granted `kms:Decrypt` and `kms:DescribeKey` scoped to that specific key ARN.
- The AWS KMS key policy must allow the AWS Glue crawler role principal to use the key. A minimal statement:
  ```json
  {
    "Sid": "Allow AWS Glue crawler",
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<account>:role/<stack-prefix>-data-GlueCrawlerRole-XXXX" },
    "Action": ["kms:Decrypt", "kms:DescribeKey"],
    "Resource": "*"
  }
  ```
- AWS KMS key rotation, deletion protection, and grant management are the customer's responsibility.
- When `IDENTITY_MAPPING=true`, the stack also creates a **dedicated** customer-managed key for the identity-map bucket (rotation enabled). Its key policy contains only the account-root statement, which delegates access control to IAM; the Lambda's `kms:Encrypt`/`kms:GenerateDataKey` and QuickSight's `kms:Decrypt` are granted on their respective IAM roles, not in the key policy. This key is **retained on teardown** (`DeletionPolicy: Retain`, the safe CMK default) - schedule its deletion manually if you want it removed (see README "Cleaning up").

### AWS Lambda and IAM Identity Center (only when identity mapping is enabled)

- The identity-map Lambda runs the AWS-managed `python3.12` runtime, outside any VPC (it calls only public AWS API endpoints over TLS: Identity Store, Athena, S3, KMS), with `ReservedConcurrentExecutions: 1`. The `checkov` CKV_AWS_117 (not-in-VPC) and CKV_AWS_116 (no DLQ) findings are suppressed inline with that rationale - a VPC would add NAT cost with no security gain, and a DLQ adds ops surface with no benefit for an idempotent daily refresh that fails closed.
- The execution role is least-privilege: `identitystore:ListUsers` is scoped to the specific Identity Store ARN (verified against the [IAM service-authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awsidentitystore.html), which lists `ListUsers` as supporting resource-level permissions). Athena and AWS Glue actions are scoped to this stack's workgroup and database; S3 to the named buckets; KMS to the one identity-map key.
- **Data minimization**: the Lambda persists only the users that actually appear in the activity data (the intersection of `SELECT DISTINCT userid` with the directory), never the customer's whole corporate directory. The full directory is held only transiently in Lambda memory during a run.
- **Cross-account Identity Center** (`IDC_ROLE_ARN`): the Lambda assumes the supplied role. The customer owns that role's trust policy and should scope it to this Lambda's execution-role ARN and grant only `identitystore:ListUsers` on the store. Using an `ExternalId` on the trust is recommended.
- **External identities**: users who authenticate via an external identity provider (Okta/Entra), AWS Builder ID, or social login are not in the Identity Store and are never resolved - they keep their `user_id` GUID. No attempt is made to fuzzy-match them.

## Identity mapping (optional)

This section consolidates the security posture of the optional user-identity-mapping feature (`IDENTITY_MAPPING=true`). See also the README [Resolving user identities](./README.md#resolving-user-identities-optional) section.

**What it does.** Resolves the report's opaque `user_id` GUIDs to human names (display name / username / email) by looking each active user up in AWS IAM Identity Center, and joins the result into the dashboard's user labels.

**Personal data implications the customer must weigh:**
- It pulls **real names and email addresses** into Amazon QuickSight SPICE. This is personal data; treat the dashboard's audience accordingly (consider Row-Level Security and dashboard-permission scoping).
- IAM Identity Center can live in a **different AWS Region** than the dashboard (`IDC_REGION`). Resolving names then performs a **cross-region transfer of personal data** into the dashboard region's bucket and SPICE. Confirm this is acceptable under your data-residency obligations before enabling.
- Mutually exclusive with `HASH_EMAILS` - the deploy script refuses to run with both, since resolving names while hashing email is contradictory and would silently defeat the privacy control.

**Containment and isolation:**
- Resolved PII lands in a **dedicated SSE-KMS bucket**, isolated from the Athena results bucket (which stays SSE-S3 for transient query scratch). The PII bucket has BPA, TLS-only, versioning, server access logging, and a noncurrent-version lifecycle.
- The map is written by a single atomic `PutObject`, so QuickSight/Athena never read a half-written file. The Lambda **fails closed**: any error leaves the previous good map in place rather than regressing the dashboard to GUIDs or partial data.

**Opt-out and teardown:**
- Re-deploying with `IDENTITY_MAPPING=false` rebuilds the views without the join, forces a SPICE full-refresh to flush resolved names from memory, **purges all versions** from the PII bucket, removes the bucket and the `<prefix>-identity-map` stack, and rewrites the QuickSight inline policy to drop the identity-map bucket and KMS grants.
- `scripts/teardown.sh` removes the identity-map stack first and purges the PII (and access-log) buckets. The KMS key is retained by design; delete it manually if required.

## Threat summary

The full threat model is reviewed internally before each release. The mitigation
status of the threats most relevant to deployers is summarized here. Status
values describe the *default configuration* this stack ships - customers may
need additional account-level controls (CloudTrail, AWS Config, organization
SCPs, network restrictions) to meet their own compliance baselines, and are
responsible for those decisions.

| ID | Threat | Mitigation status |
|----|--------|-------------------|
| T-001 | Data interception in transit between Amazon S3, Amazon Athena, and Amazon QuickSight | Implemented as default (TLS 1.2+; `aws:SecureTransport` bucket policy on results bucket) |
| T-002 | Direct access to the Athena results bucket bypassing Amazon QuickSight | Implemented as default (Block Public Access, TLS-only bucket policy, IAM scoping, 30-day lifecycle) |
| T-003 | PII (email) exposure to dashboard viewers without authorization | Customer opt-in (`HASH_EMAILS=true` at deploy; RLS guidance in README) |
| T-004 / T-008 | AWS Glue crawler IAM role over-privileged | Implemented as default (three resource-scoped inline policies; no AWS-managed policy attached) |
| T-005 | Modification of Athena views or AWS Glue table to point to unauthorized data | Implemented as default (`EnforceWorkGroupConfiguration: true`; IAM separation between crawler and catalog modification) |
| T-006 | Compromised administrator credentials exfiltrating data | Customer responsibility (CloudTrail logging recommended; S3 access logging on the results bucket can be enabled by the customer post-deploy if required) |
| T-007 | Malicious data injection into the source bucket to manipulate dashboard output | Customer responsibility (source bucket policy controls writers; this solution has read-only access) |
| T-009 | `s3:ListAllMyBuckets` on wildcard resource in the Amazon QuickSight inline policy | Accepted (AWS API design constraint; reveals only names, not contents) |
| T-010 | SQL injection via dynamic column names in `build_views.py` | Implemented as default (regex identifier validation in `_validate_identifier()`; column names sourced exclusively from the AWS Glue catalog, not user input; `# nosec B608` annotation explains why parameterization is not available for Athena DDL) |
| T-011 | Command injection via shell environment variables | Implemented partially (trust boundary is the administrator workstation; `KIRO_LOGS_BUCKET` is normalized for ARN / `s3://` / trailing-slash before use; explicit bucket-name regex validation is not yet applied) |
| T-012 | Resolved names/emails (PII) exposed to unauthorized dashboard viewers when identity mapping is on | Customer opt-in and responsibility (feature is off by default; isolated SSE-KMS bucket; RLS + dashboard-permission scoping are the customer's to configure) |
| T-013 | Cross-region / cross-account transfer of directory PII via identity mapping | Customer opt-in (documented loudly; only the active-user intersection is persisted, minimizing - not eliminating - exposure; customer owns the data-residency decision) |
| T-014 | Identity-map Lambda role over-privileged, or its SQL query injected via the database name | Implemented as default (`identitystore:ListUsers` scoped to the store ARN; Athena/Glue/S3/KMS resource-scoped; cross-account assume-role and source decrypt are conditional; the `SELECT DISTINCT userid` query interpolates only a CFN-`AllowedPattern`-validated database name, with a `# nosec B608` annotation) |
| T-015 | Stale or empty identity map silently regressing the dashboard | Implemented as default (Lambda fails closed - never overwrites a good map with empty/partial output; deploy-time seed + synchronous population; daily refresh) |
| T-016 | Resolved PII lingering after opt-out / teardown | Implemented as default (opt-out forces SPICE refresh + purges all bucket versions + deletes the stack; teardown purges and deletes the PII and log buckets) |


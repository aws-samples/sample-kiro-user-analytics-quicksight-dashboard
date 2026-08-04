# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass: run `scripts/run-checks.sh` (unit tests plus shell, CloudFormation and Python linting). It needs no AWS account, credentials or network. See [tests/README.md](./tests/README.md).
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Versioning

[`VERSION`](./VERSION) holds a single semantic version and is the source of truth. `scripts/deploy.sh` reads it and stamps it onto every stack as a `KiroAnalyticsVersion` tag and onto each QuickSight dashboard version, which is how a deployment in the field can be traced back to the code that produced it.

A contributor does **not** need to bump it — maintainers do that when cutting a release:

1. Update `VERSION`.
2. Add a matching section to [`CHANGELOG.md`](./CHANGELOG.md). A test enforces that the current `VERSION` has an entry, so a bump without a changelog entry fails CI.
3. Tag the release `v<version>`.

Choose the bump by what it costs the person upgrading, not by how large the diff is:

* **MAJOR** — a re-deploy is not sufficient (manual teardown, a newly required parameter), or a number on the dashboard changes meaning.
* **MINOR** — new sheets, visuals, datasets or opt-in features.
* **PATCH** — fixes and documentation.

If a change alters what an existing view returns, say so in the changelog entry: the datasets are SPICE import mode, so customers must refresh before they see it.


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.

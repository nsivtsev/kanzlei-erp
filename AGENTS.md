# Local acceptance for agents

- Run tests for changed modules and any directly affected dependencies. Choose checks based on the behavior and risk of the change.
- For a UI change, exercise one short end-to-end browser scenario. Add languages or viewport sizes when localization or responsive layout changed.
- Expand checks when changing shared permission, storage, migration, dependency, or build behavior. State the concrete risk that warrants the broader run.
- Do not repeat a passing check unless code changed afterward or a specific concern remains. After a fix, rerun the affected checks.
- Do not run the full local CI image build, backup/restore acceptance, or separate multi-stage acceptance cycles by default. GitHub CI remains the release gate and is not changed by this policy.
- Report the changed behavior, checks run and their results, and any meaningful unverified area briefly. Produce a long acceptance report or screenshot set only when requested.
- Preserve unrelated work already present in the checkout. The task owner creates the PR and deploys unless explicitly requested otherwise.

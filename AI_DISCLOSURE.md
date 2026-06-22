# AI Disclosure

Crossroads-UK embraces AI as a core efficiency asset of modern systems engineering. Systems built with AI in mind are easier to build, maintain and fork.

To ensure code quality and clear ownership, all development follows a structured, architect-led workflow.

## The Development Loop
Every component of this engine undergoes a rigid three-stage implementation cycle:

- **Planning**: A comprehensive engineering plan is authored and saved directly to a `*.md` file inside the `docs/plans/` directory.
- **Implementation**: The plan is passed to an AI agent framework (e.g., Claude) to build the underlying Python code and test suites strictly according to the specification.
- **Code Review**: All code is manually reviewed and verified against the initial planning specification before committing. LLM's, such as Gemini, may be used as necessary.

The developer is the Systems Architect using AI as a sounding board and execution engine. In addition, they provide hands-on engineering such as refactoring logic, debugging edge cases, and fine-tuning code when automation falls short.

**Developers are solely responsible for the correctness and defense of all of their commits.**
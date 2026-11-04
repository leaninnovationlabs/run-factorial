"""The in-app assistant's chat backends: the vendor-neutral contract, and adapters behind it.

The contract is `runtime.assistant.contract`; adapters are reached through the registry in
`runtime.assistant.adapters`, so importing the contract never drags in vendor code.
"""

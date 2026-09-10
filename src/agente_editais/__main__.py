"""Reexpoe a CLI do agente para ``python -m agente_editais``.

Permite que o dashboard (e outros invólucros) disparem a CLI em um
subprocess sem depender do script de console instalado (AD-5: o dashboard
não faz rede in-process, apenas delega a comandos do CLI).
"""

from .consulta import main

if __name__ == "__main__":
    main()

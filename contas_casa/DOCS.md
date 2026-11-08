# Contas da Casa (add-on do Home Assistant)

Contas a pagar com comprovante, entradas de dinheiro e fundos (reserva, seguro…),
para o casal lançar e acompanhar junto pelo Home Assistant.

- Abre no menu lateral do HA (ingress) e no app do HA no celular.
- Quem está logado no HA aparece como "lançado por" / "pago por".
- Contas: uma vez, todo mês (fixa) ou parcelada. Botão **Pagar** com foto do comprovante.
- Entradas do mês e fundos com guardar/retirar e meta.
- Publica sensores no HA (`sensor.contas_casa_*`, `sensor.fundo_*`) para os avisos.
- Dados em `/data` do add-on (SQLite + comprovantes), entram no backup do HA.

## Instalar
Configurações → Add-ons → Loja → ⋮ → Repositórios → adicionar a URL deste repositório,
depois instalar **Contas da Casa**, ligar "Mostrar na barra lateral" e iniciar.

O arquivo `home-assistant/packages/contas_casa.yaml` tem as automações do aviso da manhã.

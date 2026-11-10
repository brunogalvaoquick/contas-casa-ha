## 1.5.0
- Nova aba **Gastos**: lança gasto do dia já pago (mercado, farmácia, gasolina…)
  numa tela só, com forma de pagamento e comprovante/nota. Lista por dia com
  total do dia e por categoria.
- Resumo das contas mostra os gastos do mês e a sobra desconta os gastos.
- Novos sensores: sensor.contas_casa_gastos_hoje e sensor.contas_casa_gastos_mes.

## 1.4.0
- Volta a opção simples `usuarios_permitidos` na aba Configuração: escreva o
  nome ou o login de quem pode usar, um por linha. Administradores do HA
  sempre entram. Lista vazia = só administradores.
- O add-on não preenche mais a lista sozinho.

## 1.3.0
- Quem pode usar agora fica na aba **Configuração** do add-on (`usuarios`).
  O add-on preenche sozinho com os usuários do HA (liberado: false); é só
  trocar para `true` e salvar. Administradores do HA sempre entram.
- Removida a tela ⚙ Usuários de dentro do app.

## 1.2.0
- Nova tela ⚙ Usuários (só para administradores do HA): lista os usuários do HA
  e libera cada um com uma chave. Administradores sempre entram.
- Removida a opção `usuarios_permitidos` (a tela substitui).

## 1.1.0
- O painel "Contas" agora aparece também para usuários que não são administradores.
- Nova opção `usuarios_permitidos`: lista de usuários do HA que podem abrir
  (login ou nome). Vazia = todos.

## 1.0.0
- Primeira versão: contas (única, todo mês, parcelada), pagar com comprovante,
  entradas, fundos (reserva e seguro), sensores no Home Assistant.

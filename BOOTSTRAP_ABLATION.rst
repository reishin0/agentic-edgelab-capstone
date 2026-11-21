EdgeLab bootstrap ablation
=========================

Keep CUSUM as the production stability rule. The revised bootstrap
remains experimental. This study used only synthetic formation data
and did not access the market holdout.

All six arms received the same 540 observations with unchanged BIC,
ADF/KPSS and bidirectional Engle-Granger gates. Nested widening preserved
the 20%-80% grid and added outer points. The exact wider revision also
offset the interior grid slightly. Bootstrap arms shared 199 wild-sign
AR(1) paths per sample.

Counts represent advances, not trades or validated trading signals.

.. list-table::
   :header-rows: 1

   * - Scenario
     - Trials
     - CUSUM
     - Original
     - Width only
     - Intercept only
     - Both nested
     - Exact revision
   * - independent_walks
     - 400
     - 0
     - 3
     - 3
     - 1
     - 1
     - 1
   * - correlated_walks
     - 400
     - 0
     - 9
     - 9
     - 3
     - 4
     - 4
   * - ar40
     - 240
     - 124
     - 182
     - 186
     - 184
     - 187
     - 186
   * - ar92
     - 240
     - 0
     - 177
     - 176
     - 177
     - 175
     - 175
   * - level_0.3_43
     - 240
     - 58
     - 110
     - 115
     - 105
     - 110
     - 111
   * - level_0.6_15
     - 240
     - 20
     - 69
     - 37
     - 72
     - 37
     - 36
   * - level_0.6_43
     - 240
     - 19
     - 23
     - 28
     - 19
     - 22
     - 24
   * - level_0.6_78
     - 240
     - 13
     - 27
     - 26
     - 24
     - 23
     - 27
   * - level_0.6_88
     - 240
     - 21
     - 93
     - 50
     - 94
     - 45
     - 45
   * - intercept_weak_late
     - 240
     - 0
     - 123
     - 124
     - 115
     - 120
     - 59

Widening alone drove most of the improvement for the +0.6 level shifts at
15% and 88%. The intercept-only statistic did not solve small mid-window
shifts. The late break in a weak spread remained sensitive to grid alignment:
50.0% advanced under the nested combination versus 24.6% under the exact
revision. CUSUM advanced none there, but also advanced no weak stable
spreads. The study is not evidence of market or trading performance.

Readback verified 2,720 unique synthetic samples, 16,320 arm decisions and
matched contrasts. Detailed artifacts remain machine-local. SHA-256:

* Protocol: 2302a442ede48b7052c849117486a44932bcc538ca9026918a6958446a638a7a
* Runner: d7fed6c7e608dc5cac768667586ebc2215849b4d4361e30f43845f8a56d5a358
* Results: 11b5da9d05dbe21b20c626b5af27e028463769e0fc9ed73cb8be690640f87100
* Trial ledger: d84b627c9baab671d19d744308d56412a2b689b42c6374f7f2a655074e1d83e6

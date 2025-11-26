 public function atualizarConteudoPdfOCRLote(int $inicioId, int $tamanhoLote = 100)
    {
        require 'vendor/autoload.php';
        $conexao = \sistema\Nucleo\Conexao::getInstancia();

        $popplerPath   = '"C:\\Poppler\\Release-23.10.0-0\\poppler-23.10.0\\Library\\bin\\pdftoppm.exe"';
        $tesseractPath = '"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"';
        $tmpDir        = sys_get_temp_dir();

        // Buscar URLs base
        $leiCaminhos = $conexao->query("SELECT * FROM leis.lei_caminho")->fetchAll(\PDO::FETCH_ASSOC);
        $urlsPorTipo = [];
        foreach ($leiCaminhos as $c) {
            $urlsPorTipo[$c['tipo_caminho']] = rtrim($c['caminho'], '/');
        }
        $raizLeis = $urlsPorTipo[6] ?? 'https://www.guarulhos.sp.gov.br/06_prefeitura/leis/';

        // Buscar lote de leis
        $leis = $conexao->prepare("
            SELECT id_lei, arquivo_lei, tipo 
            FROM leis.lei 
            WHERE id_lei >= :id
            ORDER BY id_lei ASC 
            LIMIT $tamanhoLote
        ");
        $leis->execute([':id' => $inicioId]);
        $leis = $leis->fetchAll(\PDO::FETCH_ASSOC);

        if (!$leis) {
            echo "⚠️ Nenhum registro encontrado a partir do ID $inicioId\n";
            return;
        }

        echo "🚀 Processando lote a partir do ID $inicioId com " . count($leis) . " registros...\n";

        $downloads = [];
        foreach ($leis as $lei) {
            $arquivoRaw = $lei['arquivo_lei'] ?? '';
            $arquivoLimpo = ltrim(preg_replace('#^\.\./#', '', trim($arquivoRaw)), '/');

            // ✅ Casos especiais
            $basePath = $urlsPorTipo[$lei['tipo']] ?? $raizLeis;

            // Decretos
            if (preg_match('/^\.decretos_(\d{4})\//', $arquivoRaw, $matches)) {
                $ano = $matches[1];
                $basePath = "https://www.guarulhos.sp.gov.br/06_prefeitura/leis/decretos_$ano/";
                $arquivoLimpo = preg_replace('/^\.decretos_\d{4}\//', '', $arquivoLimpo);
            }
            // Autógrafos
            elseif ((int)$lei['tipo'] === 4) {
                if (preg_match('/^autografos(\d{3}_\d{4}_autografo\.pdf)$/', $arquivoLimpo, $m)) {
                    $arquivoLimpo = $m[1];
                }
                $basePath = $urlsPorTipo[4] ?? $raizLeis;
            }
            // Para leis, portarias, projetos: manter padrão pelo tipo
            else {
                $basePath = $urlsPorTipo[$lei['tipo']] ?? $raizLeis;
            }

            $urlPdf = rtrim($basePath, '/') . '/' . rawurlencode($arquivoLimpo);

            if (empty($arquivoLimpo) || !$basePath) {
                echo "⚠️ Lei {$lei['id_lei']} sem caminho válido\n";
                continue;
            }

            $downloads[] = [
                'id'   => $lei['id_lei'],
                'url'  => $urlPdf,
                'dest' => "$tmpDir/lei_{$lei['id_lei']}.pdf"
            ];
        }

        // Baixar PDFs em paralelo
        $mh = curl_multi_init();
        $chList = [];

        foreach ($downloads as $d) {
            $ch = curl_init($d['url']);
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
            curl_setopt($ch, CURLOPT_TIMEOUT, 120);

            $chList[$d['id']] = ['ch' => $ch, 'dest' => $d['dest'], 'url' => $d['url']];
            curl_multi_add_handle($mh, $ch);
        }

        $running = null;
        do {
            curl_multi_exec($mh, $running);
            curl_multi_select($mh);
        } while ($running > 0);

        foreach ($chList as $id => $info) {
            $conteudo = curl_multi_getcontent($info['ch']);
            $httpCode = curl_getinfo($info['ch'], CURLINFO_HTTP_CODE);
            $curlError = curl_error($info['ch']);

            if ($conteudo && $httpCode === 200) {
                file_put_contents($info['dest'], $conteudo);
                echo "📥 Lei $id baixada com sucesso | PDF: {$info['dest']} | HTTP: $httpCode\n";
            } else {
                echo "❌ Falha no download da lei $id | URL: {$info['url']} | HTTP: $httpCode | Erro cURL: $curlError\n";
            }
            curl_multi_remove_handle($mh, $info['ch']);
            curl_close($info['ch']);
        }
        curl_multi_close($mh);

        foreach ($downloads as $d) {
            $id  = $d['id'];
            $pdf = $d['dest'];
            $baseName = "$tmpDir/lei_$id";

            if (!file_exists($pdf)) {
                echo "⚠️ PDF da lei $id não encontrado, pulando...\n";
                continue;
            }

            echo "🔎 Convertendo lei $id em imagens...\n";
            $cmdPdfToPng = "$popplerPath -png -r 150 \"$pdf\" \"$baseName\" 2>nul";
            exec($cmdPdfToPng);

            $paginas = glob("$baseName-*.png");
            if (!$paginas) {
                echo "❌ Nenhuma página encontrada no PDF da lei $id\n";
                continue;
            }

            foreach ($paginas as $pagina) {
                $saida = str_replace('.png', '', $pagina);
                exec("$tesseractPath \"$pagina\" \"$saida\" -l por");
            }

            $textoCompleto = '';
            foreach (glob("$tmpDir/lei_$id-*.txt") as $txtPath) {
                if (file_exists($txtPath)) {
                    $textoCompleto .= file_get_contents($txtPath) . "\n\n";
                    @unlink($txtPath);
                }
            }

            if (!empty(trim($textoCompleto))) {
                $update = $conexao->prepare("UPDATE leis.lei SET conteudo_pdf = :texto WHERE id_lei = :id");
                $update->execute([':texto' => $textoCompleto, ':id' => $id]);
                echo "✅ Lei $id atualizada com sucesso\n";
            } else {
                echo "⚠️ Nenhum texto reconhecido na lei $id\n";
            }

            @unlink("$tmpDir/lei_$id.pdf");
            foreach (glob("$tmpDir/lei_$id-*.png") as $png) {
                @unlink($png);
            }
        }

        echo "🏁 Lote finalizado!\n";
    }
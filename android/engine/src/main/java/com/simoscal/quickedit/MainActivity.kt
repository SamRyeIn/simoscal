package com.simoscal.quickedit

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import com.simoscal.quickedit.ui.QuickEditApp
import com.simoscal.quickedit.ui.QuickEditTheme

/**
 * The app's single activity.
 *
 * Everything below it is Compose. The activity itself holds no calibration
 * state: the [QuickEditViewModel] survives configuration changes, and anything
 * that must survive *process death* is persisted through [RecoveryStore] after
 * every state-changing engine call.
 */
class MainActivity : ComponentActivity() {

    private val viewModel: QuickEditViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            QuickEditTheme {
                QuickEditApp(viewModel = viewModel)
            }
        }
    }
}

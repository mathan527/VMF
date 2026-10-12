package com.vmf.sample;

import android.app.Activity;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Tiny test app for the VMF runner: an email field, a Login button, and a Welcome message. */
public class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(48, 160, 48, 48);

        TextView title = new TextView(this);
        title.setText("VMF Sample");
        title.setTextSize(28);

        EditText email = new EditText(this);
        email.setHint("email");
        email.setContentDescription("email_input");

        Button login = new Button(this);
        login.setText("Login");
        login.setAllCaps(false);
        login.setContentDescription("login_button");

        final TextView status = new TextView(this);
        status.setTextSize(22);

        login.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                status.setText("Welcome");
            }
        });

        root.addView(title);
        root.addView(email);
        root.addView(login);
        root.addView(status);
        setContentView(root);
    }
}
